#!/usr/bin/env python3
"""带重试的 edge-tts 分段配音。

本机到 speech.platform.bing.com 的连接是间歇性的：同一分钟里可能先成功后被重置。
每段最多重试若干轮，直连优先，连续失败后改用环境代理；单次调用有硬超时，
避免某次连接挂死拖住整批。已生成且非零的 mp3 直接跳过，可反复运行补齐。
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIO = ROOT / "audio"
SCRIPT = json.loads((ROOT / "script.json").read_text(encoding="utf-8"))

# edge-tts / aiohttp 会读环境代理，进程启动时先摘掉；代理地址另存供显式重试用。
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
for k in [k for k in list(os.environ)
          if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy")]:
    os.environ.pop(k)

import edge_tts  # noqa: E402  (必须在摘掉代理变量之后导入)

VOICE = SCRIPT["voice"]
RATE = SCRIPT["rate"]
ROUNDS = 12


async def synth_one(text: str, out: Path) -> bool:
    for attempt in range(ROUNDS):
        use_proxy = attempt >= 3 and PROXY is not None
        kw = {"proxy": PROXY} if use_proxy else {}
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(text, VOICE, rate=RATE, **kw).save(str(out)),
                timeout=25,
            )
            if out.exists() and out.stat().st_size > 2000:
                return True
            out.unlink(missing_ok=True)
        except Exception as exc:
            out.unlink(missing_ok=True)
            if attempt in (0, 2, 5, 8, 11):
                print(f"      attempt {attempt + 1} {'proxy' if use_proxy else 'direct'}: "
                      f"{type(exc).__name__}: {str(exc)[:90]}", flush=True)
        await asyncio.sleep(1.2)
    return False


async def main():
    if not AUDIO.exists():
        AUDIO.mkdir(parents=True)
    pending = []
    for seg in SCRIPT["segments"]:
        out = AUDIO / f"{seg['id']}.mp3"
        if out.exists() and out.stat().st_size > 2000:
            print(f"  [{seg['id']}] 已存在，跳过", flush=True)
            continue
        ok = await synth_one(seg["narration"], out)
        print(f"  [{seg['id']}] {'OK' if ok else 'FAILED'}  {out.stat().st_size // 1024 if ok else 0} KB", flush=True)
        if not ok:
            pending.append(seg["id"])
    print("FAILED_SEGMENTS=" + (",".join(pending) if pending else "none"), flush=True)
    return 0 if not pending else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
