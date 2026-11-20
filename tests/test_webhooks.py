"""Outbound Webhook 订阅与外部 Listener 验收测试。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import BaseModel

from examples.webhook_codebuddy_listener import (
    ListenerServer,
    WakeupCoordinator,
    build_wakeup_prompt,
)
from open_ontology import (
    Actor,
    CommandCenterRegistry,
    InvariantError,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    start_command_center_server,
)
from open_ontology.webhooks import (
    OutboundWebhookDispatcher,
    WebhookFilter,
    verify_signature,
)

ADMIN = Actor(actor_id="admin-1", role="admin", clearance_tags={"*"})
APPROVER = Actor(actor_id="mgr-1", role="manager", clearance_tags={"*"})


# ---------------------------------------------------------------------------
# 1. M2 纯调度器：过滤、HMAC 签名、密文隐藏、失败隔离不阻塞主事务
# ---------------------------------------------------------------------------
def test_dispatcher_filtering_hmac_and_failure_isolation():
    received: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def flaky_sender(
        url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, str]:
        if "broken" in url:
            raise ConnectionError("connection refused")
        assert verify_signature("s3cr3t", body, headers["X-Ontology-Signature-256"])
        received.append((url, headers, json.loads(body.decode("utf-8"))))
        return 202, "accepted"

    dispatcher = OutboundWebhookDispatcher(
        sender=flaky_sender, max_retries=1, backoff_seconds=0.01
    )
    try:
        with pytest.raises(ValueError):
            dispatcher.register(world_id="w1", target_url="file:///etc/passwd")

        sub_ok = dispatcher.register(
            world_id="w1",
            target_url="https://example.invalid/hook",
            filter_spec=WebhookFilter(
                object_types=("Item",),
                field_equals={"status": "ready"},
            ),
            secret="s3cr3t",
            created_by="admin-1",
        )
        sub_broken = dispatcher.register(
            world_id="w1",
            target_url="https://broken.invalid/hook",
            filter_spec={"actions": ["mark_ready"]},
        )
        assert sub_ok.to_public_dict()["has_secret"] is True
        assert "secret" not in sub_ok.to_public_dict()
        assert "s3cr3t" not in json.dumps(sub_ok.to_public_dict())

        # 提交两条记录：第一条 draft 不命中 field_equals={"status":"ready"}；第二条 ready 命中
        dispatcher.on_post_commit(
            "w1",
            ADMIN,
            {
                "run_id": "R-1",
                "action_type": "create_item",
                "status": "COMMITTED",
                "object_diff": {
                    "object_changes": [
                        {
                            "kind": "create",
                            "object_type": "Item",
                            "object_id": "I-1",
                            "after": {"item_id": "I-1", "status": "draft", "secret_note": "TOP_SECRET"},
                        }
                    ]
                },
            },
        )
        dispatcher.on_post_commit(
            "w1",
            ADMIN,
            {
                "run_id": "R-2",
                "action_type": "mark_ready",
                "status": "COMMITTED",
                "object_diff": {
                    "object_changes": [
                        {
                            "kind": "update",
                            "object_type": "Item",
                            "object_id": "I-1",
                            "after": {"item_id": "I-1", "status": "ready", "secret_note": "TOP_SECRET"},
                        }
                    ]
                },
            },
        )
        assert dispatcher.flush(timeout=3.0)

        assert len(received) == 1
        _, _, envelope = received[0]
        assert envelope["run_id"] == "R-2"
        assert envelope["action"] == "mark_ready"
        assert envelope["changes"] == [
            {
                "kind": "update",
                "object_type": "Item",
                "object_id": "I-1",
                "matched_fields": {"status": "ready"},
            }
        ]
        # 绝不向 Webhook 荷载泄漏未匹配字段（如敏感列明文）
        assert "TOP_SECRET" not in json.dumps(envelope)

        failed = dispatcher.list_deliveries(sub_broken.sub_id)
        assert len(failed) == 1
        assert failed[0]["status"] == "FAILED"
        assert failed[0]["attempts"] == 2
        assert "ConnectionError" in failed[0]["error"]
    finally:
        dispatcher.close()


# ---------------------------------------------------------------------------
# 2. Workshop HTTP /api/worlds/{id}/webhooks + 审批通过后触发回调
# ---------------------------------------------------------------------------
class _ApprovalItem(ObjectType):
    __primary_key__ = "item_id"
    item_id: str
    status: str = "draft"


class _ApproveItemParams(BaseModel):
    item_id: str


def test_http_webhooks_crud_permission_and_approval_delivery():
    spec = OntologySpec()
    spec.role("admin")
    spec.role("manager")
    spec.role("worker")
    spec.object_type(_ApprovalItem)

    @spec.action(
        "create_item",
        params=_ApproveItemParams,
        allowed_roles=("worker", "admin"),
        revert_roles=("manager",),
    )
    def create_item(ctx: Any, params: _ApproveItemParams) -> None:
        if ctx.actor.role == "worker":
            raise InvariantError("worker requires manager override", code="APPROVAL_REQUIRED_MANAGER")
        ctx.create(_ApprovalItem(item_id=params.item_id, status="ready"))

    runtime = OntologyRuntime(spec)
    registry = CommandCenterRegistry()
    registry.register_ontology("item_model", "Item", spec)
    registry.register_workspace(
        "ws-item",
        "item_model",
        "Item World",
        runtime,
        default_actor=APPROVER,
    )
    registry.register_actor_token("tok-mgr", APPROVER, workspace_id="ws-item")
    registry.register_actor_token("tok-admin", ADMIN, workspace_id="ws-item")
    registry.register_actor_token(
        "tok-worker",
        Actor(actor_id="W-1", role="worker", clearance_tags={"*"}),
        workspace_id="ws-item",
    )

    caught_events: list[dict[str, Any]] = []
    caught_event_flag = threading.Event()
    prompts_seen: list[str] = []

    def fake_runner(ev: dict[str, Any] | None) -> int:
        prompts_seen.append(
            build_wakeup_prompt(
                world_url="http://127.0.0.1/api/worlds/ws-item",
                actor_id="W-1",
                actor_role="worker",
                event=ev,
            )
        )
        if ev is not None:
            caught_events.append(ev)
        caught_event_flag.set()
        return 0

    coordinator = WakeupCoordinator(fake_runner)
    listener = ListenerServer(("127.0.0.1", 0), coordinator, secret="hook-secret")
    listener_thread = threading.Thread(target=listener.serve_forever, daemon=True)
    listener_thread.start()
    listener_url = f"http://127.0.0.1:{listener.server_address[1]}/webhook"

    handle = start_command_center_server(registry, port=0, title="Webhook Test")
    base = handle.base_url

    def _http(
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        token: str = "tok-mgr",
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {token}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        # worker 无权管理 webhook -> 403
        with pytest.raises(urllib.error.HTTPError) as exc403:
            _http(
                "/api/worlds/ws-item/webhooks",
                method="POST",
                payload={"target_url": listener_url},
                token="tok-worker",
            )
        assert exc403.value.code == 403
        exc403.value.close()

        # manager 注册 webhook（带签名与过滤）
        created_sub = _http(
            "/api/worlds/ws-item/webhooks",
            method="POST",
            payload={
                "target_url": listener_url,
                "secret": "hook-secret",
                "filter": {"object_types": ["_ApprovalItem"], "field_equals": {"status": "ready"}},
            },
            token="tok-mgr",
        )
        sub_id = created_sub["subscription"]["sub_id"]
        assert created_sub["subscription"]["has_secret"] is True

        # worker 提交受阻触发 REJECTED（HTTP 400），不应触发 COMMITTED webhook
        with pytest.raises(urllib.error.HTTPError) as exc400:
            _http(
                "/api/worlds/ws-item/actions/create_item",
                method="POST",
                payload={"item_id": "IT-1"},
                token="tok-worker",
            )
        assert exc400.value.code == 400
        exc400.value.close()
        assert registry.webhooks.flush(timeout=2.0)
        assert not caught_event_flag.is_set()

        # admin 同步提交 -> 触发 webhook 到达 listener 并唤醒执行器
        committed = _http(
            "/api/worlds/ws-item/actions/create_item",
            method="POST",
            payload={"item_id": "IT-1"},
            token="tok-admin",
        )
        assert committed["run"]["status"] == "COMMITTED"
        assert caught_event_flag.wait(timeout=3.0)
        assert registry.webhooks.flush(timeout=2.0)

        assert len(caught_events) == 1
        assert caught_events[0]["action"] == "create_item"
        assert caught_events[0]["changes"][0]["object_id"] == "IT-1"
        assert "_ApprovalItem:IT-1" in prompts_seen[0]

        deliveries = _http(f"/api/worlds/ws-item/webhooks/{sub_id}/deliveries")["deliveries"]
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "DELIVERED"
        assert deliveries[0]["http_status"] == 202

        # 退订后不再投递
        _http(f"/api/worlds/ws-item/webhooks/{sub_id}", method="DELETE")
        assert _http("/api/worlds/ws-item/webhooks")["count"] == 0

        _http(
            "/api/worlds/ws-item/actions/create_item",
            method="POST",
            payload={"item_id": "IT-2"},
            token="tok-admin",
        )
        assert registry.webhooks.flush(timeout=2.0)
        assert len(caught_events) == 1
    finally:
        handle.stop()
        listener.shutdown()
        listener.server_close()
        runtime.close()


# ---------------------------------------------------------------------------
# 3. 外部 Listener 并发防重入与边沿合并
# ---------------------------------------------------------------------------
def test_wakeup_coordinator_coalesces_events_while_busy():
    gate = threading.Event()
    idle_done = threading.Event()
    calls: list[Any] = []

    def slow_runner(ev: dict[str, Any] | None) -> int:
        calls.append((ev or {}).get("event_id"))
        if len(calls) == 1:
            gate.wait(timeout=2.0)
        return 0

    coord = WakeupCoordinator(slow_runner, on_idle_finished=idle_done.set)
    assert coord.notify({"event_id": "e1"}) == "STARTED"
    assert coord.notify({"event_id": "e2"}) == "COALESCED"
    assert coord.notify({"event_id": "e3"}) == "COALESCED"

    gate.set()
    assert idle_done.wait(timeout=3.0)
    # 第一轮处理 e1，忙时收到 e2/e3 合并成一次后续补扫 e3（绝不丢且绝不并发启动 3 个进程）
    assert calls == ["e1", "e3"]
    assert coord.invocations == 2
    assert coord.coalesced == 2
