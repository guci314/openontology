"""极简「整数」外部 REST 服务（自带 OpenAPI 3.0 Schema）。

用于在指挥台中测试「注册外部符号设备（OpenAPI）」。

运行::

    python3 -m examples.integer_rest_service --port 8767
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence
from urllib.parse import urlparse

_INTEGERS: dict[str, int] = {
    "INT-1": 10,
    "INT-2": 20,
    "INT-3": 30,
    "INT-4": 40,
    "INT-5": 50,
}

_WEBHOOKS: list[str] = []
_WEBHOOK_LOGS: list[dict[str, Any]] = []
_WEBHOOK_LOCK = threading.RLock()


def _dispatch_webhooks(payload: dict[str, Any]) -> None:
    """异步向所有已注册的 Webhook URL 推送变更事件。"""
    with _WEBHOOK_LOCK:
        targets = list(_WEBHOOKS)
    if not targets:
        return

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _send(url: str) -> None:
        status_code: int | None = None
        err: str | None = None
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status_code = int(getattr(resp, "status", 200))
        except Exception as exc:
            err = str(exc)
        with _WEBHOOK_LOCK:
            _WEBHOOK_LOGS.append(
                {
                    "url": url,
                    "event": payload.get("event"),
                    "status": status_code,
                    "error": err,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            )
            if len(_WEBHOOK_LOGS) > 20:
                del _WEBHOOK_LOGS[:-20]

    for target in targets:
        threading.Thread(target=_send, args=(target,), daemon=True).start()


def build_openapi_schema(base_url: str) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Integer REST Service",
            "version": "1.0.0",
            "description": "极简整数外部 REST 服务，支持查询整数与加法操作。",
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/integers": {
                "get": {
                    "operationId": "listIntegers",
                    "summary": "查询全部整数",
                    "responses": {
                        "200": {
                            "description": "整数列表",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "items": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "id": {"type": "string"},
                                                        "value": {"type": "integer"},
                                                    },
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/integers/{id}/add": {
                "post": {
                    "operationId": "addInteger",
                    "summary": "对指定整数执行加法",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"n": {"type": "integer"}},
                                    "required": ["n"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "更新后的整数",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "string"},
                                            "value": {"type": "integer"},
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/webhooks": {
                "get": {
                    "operationId": "listWebhooks",
                    "summary": "查询已注册的 Webhook 回调地址及投递记录",
                    "responses": {"200": {"description": "Webhook 列表"}},
                },
                "post": {
                    "operationId": "registerWebhook",
                    "summary": "注册 Webhook 回调地址（整数变更时自动 POST 推送）",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"url": {"type": "string"}},
                                    "required": ["url"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "已注册的 Webhook"}},
                },
            },
        },
    }


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            accept = self.headers.get("Accept") or ""
            items = [{"id": k, "value": v} for k, v in _INTEGERS.items()]
            if "application/json" in accept and "text/html" not in accept:
                self._send_json(
                    200,
                    {
                        "service": "integer-rest-service",
                        "endpoints": ["/openapi.json", "/integers", "/integers/{id}/add", "/health"],
                        "items": items,
                    },
                )
                return
            rows = "".join(
                f"<tr><td><code>{item['id']}</code></td><td><b>{item['value']}</b></td>"
                f"<td><button onclick=\"addInt('{item['id']}', 1)\">+1</button> "
                f"<button onclick=\"addInt('{item['id']}', 10)\">+10</button></td></tr>"
                for item in items
            )
            with _WEBHOOK_LOCK:
                wh_list = list(_WEBHOOKS)
                wh_logs = list(reversed(_WEBHOOK_LOGS[-5:]))
            wh_items = "".join(
                f"<li><code>{u}</code> <button onclick=\"delWh('{u}')\">移除</button></li>"
                for u in wh_list
            ) or "<li>暂无订阅</li>"
            log_items = "".join(
                f"<li>[{lg['at']}] <code>{lg['url']}</code> → {lg['status'] or lg['error']}</li>"
                for lg in wh_logs
            ) or "<li>暂无投递记录</li>"
            self._send_html(
                200,
                f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>整数 REST 设备服务</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#111}}
table{{border-collapse:collapse;width:100%;margin-top:16px}}th,td{{border:1px solid #ddd;padding:8px 12px;text-align:left}}
th{{background:#f5f5f5}}a{{color:#2563eb}}button{{padding:4px 10px;cursor:pointer}}
input[type=text]{{width:420px;padding:6px 8px}}</style></head>
<body><h2>整数 REST 设备服务（OpenAPI + Webhook）</h2>
<p>接口：<a href="/openapi.json">/openapi.json</a> · <a href="/integers">/integers</a> · <a href="/webhooks">/webhooks</a> · <a href="/health">/health</a></p>
<table><thead><tr><th>ID</th><th>当前值</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table>
<h3>Webhook 订阅</h3>
<form onsubmit="addWh(event)"><input id="whUrl" type="text" placeholder="http://127.0.0.1:8765/api/adapter/webhook?adapter_id=adp-int" required> <button type="submit">注册 Webhook</button></form>
<ul>{wh_items}</ul>
<h4>最近投递记录</h4>
<ul>{log_items}</ul>
<script>
async function addInt(id,n){{await fetch('/integers/'+encodeURIComponent(id)+'/add',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{n}})}});location.reload();}}
async function addWh(e){{e.preventDefault();const url=document.getElementById('whUrl').value.trim();if(!url)return;await fetch('/webhooks',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{url}})}});location.reload();}}
async function delWh(url){{await fetch('/webhooks',{{method:'DELETE',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{url}})}});location.reload();}}
</script>
</body></html>""",
            )
            return
        if path == "/webhooks":
            with _WEBHOOK_LOCK:
                self._send_json(
                    200,
                    {"webhooks": list(_WEBHOOKS), "recent_deliveries": list(_WEBHOOK_LOGS)},
                )
            return
        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        if path == "/openapi.json":
            host = self.headers.get("Host") or "127.0.0.1:8767"
            self._send_json(200, build_openapi_schema(f"http://{host}"))
            return
        if path == "/integers":
            self._send_json(
                200,
                {"items": [{"id": k, "value": v} for k, v in _INTEGERS.items()]},
            )
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "/webhooks":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            url = str(body.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._send_json(400, {"error": "url must start with http:// or https://"})
                return
            with _WEBHOOK_LOCK:
                if url not in _WEBHOOKS:
                    _WEBHOOKS.append(url)
                hooks = list(_WEBHOOKS)
            self._send_json(200, {"ok": True, "webhooks": hooks})
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "integers" and parts[2] == "add":
            obj_id = parts[1]
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            n = int(body["n"])
            _INTEGERS[obj_id] = _INTEGERS.get(obj_id, 0) + n
            _dispatch_webhooks(
                {
                    "event": "integer.updated",
                    "device": "integer-rest-service",
                    "item": {"id": obj_id, "value": _INTEGERS[obj_id]},
                    "items": [{"id": k, "value": v} for k, v in _INTEGERS.items()],
                }
            )
            self._send_json(200, {"id": obj_id, "value": _INTEGERS[obj_id]})
            return
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/")
        if path == "/webhooks":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            url = str(body.get("url") or "").strip()
            with _WEBHOOK_LOCK:
                if url in _WEBHOOKS:
                    _WEBHOOKS.remove(url)
                hooks = list(_WEBHOOKS)
            self._send_json(200, {"ok": True, "webhooks": hooks})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="极简整数 REST 服务（OpenAPI）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--webhook", action="append", default=[], help="初始注册的 Webhook 回调 URL")
    args = parser.parse_args(list(argv) if argv is not None else None)
    with _WEBHOOK_LOCK:
        for url in args.webhook:
            if url and url not in _WEBHOOKS:
                _WEBHOOKS.append(url)

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"Integer REST Service ready → http://{args.host}:{args.port} (OpenAPI: /openapi.json)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
