#!/usr/bin/env python3
"""轻量 CDP 客户端：启动 headless Chrome，控制页面并逐帧截图。

只依赖 Python 内置库 + websockets（本机已装 16.1.1）。
不走环境代理：DevTools 端口在本机，urllib 显式禁用 ProxyHandler。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class CDP:
    def __init__(self, proc, ws, opener):
        self.proc = proc
        self.ws = ws
        self.opener = opener
        self._id = 0

    @classmethod
    def open(cls, port: int = 9333, width: int = 1920, height: int = 1080):
        profile = tempfile.mkdtemp(prefix=f"archify-cdp-{port}-")
        proc = subprocess.Popen(
            [
                CHROME,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--allow-file-access-from-files",
                "--autoplay-policy=no-user-gesture-required",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                opener.open(f"{base}/json/version", timeout=1).read()
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError(f"Chrome 提前退出，code={proc.returncode}")
                time.sleep(0.2)
        else:
            proc.terminate()
            raise RuntimeError("Chrome DevTools 端口未就绪")

        targets = json.loads(opener.open(f"{base}/json/list", timeout=2).read().decode())
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            proc.terminate()
            raise RuntimeError("没有可用的 page target")

        # 环境里有 HTTP_PROXY/HTTPS_PROXY；websockets 会读它们把本机 CDP 连接
        # 也送进代理，代理不支持 ws upgrade，连接会被静默掐断。临时清掉。
        proxy_keys = [k for k in list(os.environ) if k.lower() in
                      ("http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy")]
        saved_proxy = {k: os.environ.pop(k) for k in proxy_keys}
        try:
            ws = connect(page["webSocketDebuggerUrl"], max_size=None,
                         open_timeout=10, proxy=None)
        finally:
            os.environ.update(saved_proxy)
        cli = cls(proc, ws, opener)
        cli.send("Page.enable")
        cli.send("Runtime.enable")
        cli.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        return cli

    def send(self, method: str, params: dict | None = None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP {method}: {msg['error']}")
            return msg.get("result", {})

    def eval(self, expression: str, await_promise: bool = False):
        r = self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        )
        if "exceptionDetails" in r:
            raise RuntimeError(f"页面脚本异常: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    def goto(self, file_path: str):
        url = "file://" + str(Path(file_path).resolve())
        self.send("Page.navigate", {"url": url})
        for _ in range(80):
            if self.eval("document.readyState") == "complete":
                time.sleep(0.35)
                return
            time.sleep(0.15)
        raise RuntimeError("页面加载超时")

    def screenshot(self, path: str):
        r = self.send(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False, "fromSurface": True},
        )
        Path(path).write_bytes(base64.b64decode(r["data"]))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
