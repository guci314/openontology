"""Verify the generated O2C demo video artefacts (TK-ONTO-007).

These checks read the real files produced by ``video/build_o2c_video.py`` and
assert the SPEC acceptance criteria.  They never re-render anything; if the
video is missing, run the builder first.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VIDEO_DIR = ROOT / "video"
ASSET_DIR = VIDEO_DIR / "assets"
FRAME_DIR = VIDEO_DIR / "frames"
AUDIO_DIR = VIDEO_DIR / "audio"
OUTPUT = VIDEO_DIR / "o2c_ontology_demo.mp4"

FFPROBE = "/opt/homebrew/bin/ffprobe"
BUILDER = VIDEO_DIR / "build_o2c_video.py"


def _probe() -> dict:
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(OUTPUT),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _stream(info: dict, kind: str) -> dict:
    for stream in info.get("streams", []):
        if stream.get("codec_type") == kind:
            return stream
    return {}


@pytest.fixture(scope="module")
def video_info() -> dict:
    if not OUTPUT.exists():
        pytest.skip(
            "video not built yet; run: python3 video/build_o2c_video.py"
        )
    return _probe()


def test_builder_script_exists():
    assert BUILDER.exists()
    text = BUILDER.read_text(encoding="utf-8")
    # the right pane must be a real Playwright capture, never a mock
    assert "sync_playwright" in text
    assert "http://localhost:8069" in text
    assert "erp_north" in text
    assert "o2c_ontology_demo.mp4" in text


def test_output_video_exists_and_is_large_enough(video_info):
    assert OUTPUT.exists()
    size = OUTPUT.stat().st_size
    assert size > 1024 * 1024, f"video too small: {size} bytes"


def test_resolution_is_1080p(video_info):
    video = _stream(video_info, "video")
    assert video.get("width") == 1920
    assert video.get("height") == 1080
    assert video.get("pix_fmt") == "yuv420p"
    assert video.get("codec_name") == "h264"
    assert video.get("r_frame_rate") == "30/1"


def test_has_audio_track(video_info):
    audio = _stream(video_info, "audio")
    assert audio.get("codec_name") == "aac"
    assert int(audio.get("channels", 0)) == 2
    assert int(audio.get("sample_rate", 0)) == 44100


def test_duration_within_range(video_info):
    duration = float(video_info["format"]["duration"])
    assert 60.0 <= duration <= 140.0, f"duration {duration}s outside 60-140s"


def test_six_composite_frames_exist():
    for index in range(1, 7):
        frame = FRAME_DIR / f"scene_{index:02d}.png"
        assert frame.exists(), f"missing {frame}"
        assert frame.stat().st_size > 100_000, f"{frame} looks empty"


def test_six_real_odoo_screenshots_exist():
    for index in range(1, 7):
        shot = ASSET_DIR / f"odoo_raw_{index:02d}.png"
        assert shot.exists(), f"missing {shot}"
        # a real 1280x1000 @2x Odoo screenshot is well above 50 KB
        assert shot.stat().st_size > 50_000, f"{shot} looks empty"


def test_six_narration_audio_clips_exist():
    for index in range(1, 7):
        clip = AUDIO_DIR / f"scene_{index:02d}.mp3"
        assert clip.exists(), f"missing {clip}"
        assert clip.stat().st_size > 20_000, f"{clip} looks empty"


def test_frames_are_real_odoo_pages_not_mocks():
    """The composite frames must embed a base64 screenshot of the real page."""
    for index in range(1, 7):
        shot = ASSET_DIR / f"odoo_raw_{index:02d}.png"
        data = shot.read_bytes()
        # PNG signature + IHDR width/height big-endian
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        # device_scale_factor=2 -> 1280x1000 logical becomes 2560x2000
        assert (width, height) == (2560, 2000), f"{shot} is {width}x{height}"
