"""示例级外部 Outbound Webhook Listener：监听世界变更事件并唤醒本地空闲 CodeBuddy。

架构定位：
* 本脚本完全属于 **仓库外置消费示例**，不属于 ``open_ontology`` 内核或宿体。
* 服务端通过 ``POST <target_url>`` 发送通用标准事件信封，不感知也不依赖 CodeBuddy。
* 本 Listener 负责：
  1. 启动时向 ``<WORLD_URL>/webhooks`` 注册订阅（例如只监听 ``Card.status == "ready"``）；
  2. 收到 POST 后验证 HMAC-SHA256 签名，立即返回 ``202 Accepted``；
  3. 用线程锁做并发防重入与边沿合并：若本地 CodeBuddy 空闲则立即唤醒 ``codebuddy -p ... -y``
     自发现 OpenAPI 领卡交付；若正在忙碌则记 ``pending_wakeup=True``，等本轮完成后再补扫一次。

示例启动命令：
    python3 examples/webhook_codebuddy_listener.py \\
      --world-url http://127.0.0.1:8765/api/worlds/board-prod \\
      --actor-id M-DEV-01 --actor-role developer \\
      --admin-id board-manager-01 --admin-role board_manager \\
      --secret dev-secret
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


def _verify_sig(secret: str, body: bytes, header_val: str) -> bool:
    if not secret:
        return True
    if not header_val:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header_val.strip())


def build_wakeup_prompt(
    *,
    world_url: str,
    actor_id: str,
    actor_role: str,
    actor_token: str = "",
    skill_path: str = "skills/ontology-worker/SKILL.md",
    event: dict[str, Any] | None = None,
) -> str:
    """构造交给空闲外部执行器的最小唤醒指令（不包含写死的业务 Action）。"""
    auth_hint = (
        f"ACTOR_TOKEN（使用 Authorization: Bearer <token>）已分配"
        if actor_token
        else f"ACTOR_ID={actor_id} ，ACTOR_ROLE={actor_role}"
    )
    hint_changes = ""
    if event:
        changes = event.get("changes") or []
        summary = ", ".join(
            f"{c.get('object_type')}:{c.get('object_id')}" for c in changes[:5]
        )
        if summary:
            hint_changes = f"（触发变更线索：{summary}）"
    return (
        f"请先阅读 {skill_path} 并严格遵循。"
        f"你的上岗配置：WORLD_URL={world_url} ，{auth_hint}。"
        f"世界刚刚发生更新{hint_changes}。"
        "请拉取 openapi.json，查询当前你所在角色可认领的最优先 1 项工作并认领执行，"
        "完成后把真实结果通过 OpenAPI 写回世界状态。"
    )


class WakeupCoordinator:
    """忙闲感知的外部唤醒合并器：不并发拉起多个同类 Worker，不丢唤醒边缘。"""

    def __init__(
        self,
        runner_fn: Callable[[dict[str, Any] | None], int | None],
        *,
        on_idle_finished: Callable[[], None] | None = None,
    ) -> None:
        self._runner_fn = runner_fn
        self._on_idle_finished = on_idle_finished
        self._lock = threading.Lock()
        self._running = False
        self._pending = False
        self._last_event: dict[str, Any] | None = None
        self.invocations: int = 0
        self.coalesced: int = 0

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def notify(self, event: dict[str, Any] | None = None) -> str:
        """收到 Webhook 通知时调用：空闲则异步启动，忙碌则合并等待下次补扫。"""
        with self._lock:
            self._last_event = event
            if self._running:
                self._pending = True
                self.coalesced += 1
                return "COALESCED"
            self._running = True
            self._pending = False
            thread = threading.Thread(
                target=self._worker_loop,
                args=(event,),
                name="codebuddy-wakeup-worker",
                daemon=True,
            )
            thread.start()
            return "STARTED"

    def _worker_loop(self, initial_event: dict[str, Any] | None) -> None:
        event = initial_event
        try:
            while True:
                with self._lock:
                    self.invocations += 1
                try:
                    self._runner_fn(event)
                except Exception:
                    pass
                with self._lock:
                    if not self._pending:
                        self._running = False
                        break
                    self._pending = False
                    event = self._last_event
        finally:
            if self._on_idle_finished is not None:
                self._on_idle_finished()


class ListenerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        coordinator: WakeupCoordinator,
        *,
        secret: str = "",
    ) -> None:
        self.coordinator = coordinator
        self.secret = secret
        super().__init__(server_address, _WebhookHandler)


class _WebhookHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "running": self.server.coordinator.is_running,  # type: ignore[attr-defined]
                    "invocations": self.server.coordinator.invocations,  # type: ignore[attr-defined]
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b"{}"
        secret = getattr(self.server, "secret", "")
        sig = self.headers.get("X-Ontology-Signature-256", "")
        if not _verify_sig(secret, body, sig):
            self._send_json(401, {"ok": False, "error": "invalid webhook signature"})
            return
        try:
            event = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid JSON"})
            return
        decision = self.server.coordinator.notify(event)  # type: ignore[attr-defined]
        self._send_json(202, {"ok": True, "decision": decision})

    def log_message(self, format: str, *args: Any) -> None:
        return


def _register_subscription(
    *,
    world_url: str,
    target_url: str,
    secret: str,
    admin_id: str,
    admin_role: str,
    admin_token: str,
    object_type: str,
    field_equals: dict[str, Any],
) -> str:
    payload = {
        "target_url": target_url,
        "secret": secret,
        "description": "External CodeBuddy wakeup listener",
        "filter": {
            "object_types": [object_type] if object_type else [],
            "field_equals": field_equals,
        },
    }
    headers = {"Content-Type": "application/json"}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    else:
        headers["X-Actor-Id"] = admin_id
        headers["X-Actor-Role"] = admin_role
    req = urllib.request.Request(
        world_url.rstrip("/") + "/webhooks",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    return str(res["subscription"]["sub_id"])


def _delete_subscription(
    *,
    world_url: str,
    sub_id: str,
    admin_id: str,
    admin_role: str,
    admin_token: str,
) -> None:
    headers: dict[str, str] = {}
    if admin_token:
        headers["Authorization"] = f"Bearer {admin_token}"
    else:
        headers["X-Actor-Id"] = admin_id
        headers["X-Actor-Role"] = admin_role
    req = urllib.request.Request(
        f"{world_url.rstrip('/')}/webhooks/{sub_id}",
        headers=headers,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.URLError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="External Outbound Webhook listener waking idle CodeBuddy."
    )
    parser.add_argument(
        "--world-url",
        default="http://127.0.0.1:8765/api/worlds/board-prod",
        help="Target world base URL.",
    )
    parser.add_argument("--listen", default="127.0.0.1:8790", help="host:port")
    parser.add_argument("--public-url", default="", help="Callback URL seen by server.")
    parser.add_argument("--actor-id", default="M-DEV-01")
    parser.add_argument("--actor-role", default="developer")
    parser.add_argument("--actor-token", default="")
    parser.add_argument("--admin-id", default="board-manager-01")
    parser.add_argument("--admin-role", default="board_manager")
    parser.add_argument("--admin-token", default="")
    parser.add_argument("--secret", default="")
    parser.add_argument("--object-type", default="Card")
    parser.add_argument("--watch-status", default="ready")
    parser.add_argument("--skill", default="skills/ontology-worker/SKILL.md")
    parser.add_argument("--workdir", default=str(Path.cwd()))
    parser.add_argument("--executable", default="codebuddy")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print wakeup prompt instead of executing codebuddy CLI.",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Do not auto-register/unregister webhook on world server.",
    )
    args = parser.parse_args(argv)

    host, _, port_str = args.listen.rpartition(":")
    port = int(port_str or "8790")
    bind_host = host or "127.0.0.1"
    callback_url = args.public_url or f"http://{bind_host}:{port}/webhook"

    def _run_codebuddy(event: dict[str, Any] | None) -> int:
        prompt = build_wakeup_prompt(
            world_url=args.world_url,
            actor_id=args.actor_id,
            actor_role=args.actor_role,
            actor_token=args.actor_token,
            skill_path=args.skill,
            event=event,
        )
        if args.dry_run:
            print(f"[dry-run wakeup] {prompt}", flush=True)
            return 0
        proc = subprocess.run(
            [args.executable, "-p", prompt, "-y"],
            cwd=args.workdir,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        print(f"[codebuddy exit={proc.returncode}]\n{proc.stdout}", flush=True)
        return proc.returncode

    coordinator = WakeupCoordinator(_run_codebuddy)
    server = ListenerServer((bind_host, port), coordinator, secret=args.secret)

    sub_id = ""
    if not args.no_register:
        field_filter = {"status": args.watch_status} if args.watch_status else {}
        sub_id = _register_subscription(
            world_url=args.world_url,
            target_url=callback_url,
            secret=args.secret,
            admin_id=args.admin_id,
            admin_role=args.admin_role,
            admin_token=args.admin_token,
            object_type=args.object_type,
            field_equals=field_filter,
        )
        print(f"Registered webhook {sub_id} -> {callback_url}", flush=True)

    print(f"Webhook listener ready on http://{bind_host}:{port}/webhook", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if sub_id and not args.no_register:
            _delete_subscription(
                world_url=args.world_url,
                sub_id=sub_id,
                admin_id=args.admin_id,
                admin_role=args.admin_role,
                admin_token=args.admin_token,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
