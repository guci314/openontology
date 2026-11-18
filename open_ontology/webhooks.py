"""通用 Outbound Webhook 事件订阅与异步投递引擎（M2，纯领域无关、传输无关）。

设计约束：
1. 本模块只负责事件信封构造、订阅过滤、HMAC-SHA256 签名、异步排队与重试调度；
   实际网络发送函数 ``sender`` 由传输边界（例如 ``workshop.py``）注入，本模块本身
   不耦合任何 HTTP 库或特定外部 Agent 协议。
2. **只作唤醒信号，不泄密、不阻塞主事务**：
   - 信封只包含变更索引（``object_type``, ``object_id``, ``kind``）与安全匹配字段摘要，
     不直接外发对象全量敏感列；订阅者须凭自身 ``Actor`` 身份回查 OpenAPI；
   - 投递通过后台守护线程异步执行，任何网络失败只记录到投递账本，绝不影响已提交事务。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Literal

from .spec import Actor

__all__ = [
    "WebhookFilter",
    "WebhookSubscription",
    "WebhookDelivery",
    "OutboundWebhookDispatcher",
    "sign_payload",
    "verify_signature",
    "validate_webhook_url",
    "build_event_envelope",
]

ChangeKind = Literal["create", "update", "delete"]
WebhookSender = Callable[[str, dict[str, str], bytes, float], tuple[int, str]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_webhook_url(url: str) -> str:
    """校验订阅回调地址仅使用标准 HTTP(S) 网络协议。"""
    clean = str(url or "").strip()
    scheme, sep, rest = clean.partition("://")
    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not sep or scheme.lower() not in ("http", "https") or not authority or " " in clean:
        raise ValueError(
            f"invalid webhook target_url {url!r}: http/https URL with host required"
        )
    return clean


def sign_payload(secret: str, body: bytes) -> str:
    """生成 ``sha256=<hex>`` 签名头取值。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """常量时间校验 ``X-Ontology-Signature-256`` 签名。"""
    if not secret or not signature_header:
        return False
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature_header.strip())


@dataclass(frozen=True)
class WebhookFilter:
    """通用事件过滤规则（全部可选，非空条件按 AND 组合）。"""

    actions: tuple[str, ...] = ()
    object_types: tuple[str, ...] = ()
    kinds: tuple[ChangeKind, ...] = ()
    field_equals: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "WebhookFilter":
        data = raw or {}
        actions = tuple(str(x).strip() for x in (data.get("actions") or ()) if str(x).strip())
        object_types = tuple(
            str(x).strip() for x in (data.get("object_types") or ()) if str(x).strip()
        )
        kinds_raw = tuple(str(x).strip() for x in (data.get("kinds") or ()) if str(x).strip())
        for k in kinds_raw:
            if k not in ("create", "update", "delete"):
                raise ValueError(f"invalid webhook filter kind: {k!r}")
        field_equals_raw = data.get("field_equals") or {}
        if not isinstance(field_equals_raw, dict):
            raise ValueError("filter.field_equals must be a JSON object")
        return cls(
            actions=actions,
            object_types=object_types,
            kinds=kinds_raw,  # type: ignore[arg-type]
            field_equals=dict(field_equals_raw),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": list(self.actions),
            "object_types": list(self.object_types),
            "kinds": list(self.kinds),
            "field_equals": dict(self.field_equals),
        }

    def match_run(self, run: dict[str, Any]) -> list[dict[str, Any]] | None:
        """若 ``run`` 命中过滤规则，返回精简的 ``changes`` 列表；否则返回 ``None``。"""
        action_name = str(run.get("action_type") or "")
        if self.actions and action_name not in self.actions:
            return None

        diff = run.get("object_diff") or {}
        raw_changes = diff.get("object_changes") or []
        matched: list[dict[str, Any]] = []

        for ch in raw_changes:
            kind = ch.get("kind")
            obj_type = str(ch.get("object_type") or "")
            obj_id = str(ch.get("object_id") or "")
            if self.kinds and kind not in self.kinds:
                continue
            if self.object_types and obj_type not in self.object_types:
                continue

            matched_fields: dict[str, Any] = {}
            if self.field_equals:
                after = ch.get("after")
                if not isinstance(after, dict):
                    continue
                if any(after.get(k) != v for k, v in self.field_equals.items()):
                    continue
                matched_fields = {k: after.get(k) for k in self.field_equals}

            entry: dict[str, Any] = {
                "kind": kind,
                "object_type": obj_type,
                "object_id": obj_id,
            }
            if matched_fields:
                entry["matched_fields"] = matched_fields
            matched.append(entry)

        # 若声明了对象级或字段级条件但无一变更命中，整体不触发。
        if (self.object_types or self.kinds or self.field_equals) and not matched:
            return None

        if not matched:
            # 仅按 actions 匹配（或通用全量订阅）时，仍附上全部变更的最小索引。
            for ch in raw_changes:
                matched.append(
                    {
                        "kind": ch.get("kind"),
                        "object_type": str(ch.get("object_type") or ""),
                        "object_id": str(ch.get("object_id") or ""),
                    }
                )
        return matched


@dataclass
class WebhookSubscription:
    """单个世界状态下的 Outbound Webhook 订阅条目。"""

    sub_id: str
    world_id: str
    target_url: str
    filter: WebhookFilter = field(default_factory=WebhookFilter)
    secret: str = ""
    description: str = ""
    enabled: bool = True
    created_by: str = ""
    created_at: str = field(default_factory=_utcnow)

    def to_public_dict(self) -> dict[str, Any]:
        """公开序列化视图：永不回显 ``secret`` 明文。"""
        return {
            "sub_id": self.sub_id,
            "world_id": self.world_id,
            "target_url": self.target_url,
            "filter": self.filter.to_dict(),
            "has_secret": bool(self.secret),
            "description": self.description,
            "enabled": self.enabled,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass
class WebhookDelivery:
    """单次异步投递结果记录。"""

    delivery_id: str
    sub_id: str
    event_id: str
    world_id: str
    target_url: str
    status: Literal["DELIVERED", "FAILED"]
    attempts: int
    http_status: int | None = None
    error: str = ""
    delivered_at: str = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "sub_id": self.sub_id,
            "event_id": self.event_id,
            "world_id": self.world_id,
            "target_url": self.target_url,
            "status": self.status,
            "attempts": self.attempts,
            "http_status": self.http_status,
            "error": self.error,
            "delivered_at": self.delivered_at,
        }


def build_event_envelope(
    *,
    world_id: str,
    actor: Actor,
    run: dict[str, Any],
    changes: list[dict[str, Any]],
    event_id: str | None = None,
) -> dict[str, Any]:
    """构造最小化、不含敏感字段明文的标准世界变更事件信封。"""
    return {
        "event_id": event_id or f"evt-{uuid.uuid4().hex[:12]}",
        "event_type": "world.action.committed",
        "world_id": world_id,
        "run_id": run.get("run_id"),
        "action": run.get("action_type"),
        "actor": {"actor_id": actor.actor_id, "role": actor.role},
        "occurred_at": run.get("executed_at") or _utcnow(),
        "changes": changes,
    }


def _noop_sender(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, str]:
    raise RuntimeError(f"no webhook HTTP sender configured for {url}")


@dataclass(frozen=True)
class _DeliveryTask:
    subscription: WebhookSubscription
    delivery_id: str
    envelope: dict[str, Any]


class OutboundWebhookDispatcher:
    """线程安全的 Outbound Webhook 注册、匹配与后台异步投递调度器。"""

    def __init__(
        self,
        sender: WebhookSender | None = None,
        *,
        timeout: float = 5.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.05,
        max_queue_size: int = 1024,
        max_delivery_history: int = 200,
    ) -> None:
        self._sender: WebhookSender = sender or _noop_sender
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_seconds = max(0.0, float(backoff_seconds))
        self._max_delivery_history = max(10, int(max_delivery_history))

        self._lock = threading.RLock()
        self._subscriptions: dict[str, WebhookSubscription] = {}
        self._deliveries: dict[str, deque[WebhookDelivery]] = {}
        self._queue: queue.Queue[_DeliveryTask | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self._closed = False
        self._worker = threading.Thread(
            target=self._run_worker,
            name="open-ontology-webhook-dispatcher",
            daemon=True,
        )
        self._worker.start()

    # -- lifecycle / sender injection --------------------------------------
    def set_sender(self, sender: WebhookSender) -> None:
        with self._lock:
            self._sender = sender

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=timeout)

    def flush(self, timeout: float = 5.0) -> bool:
        """等待当前队列中的投递任务全部处理完毕（主要供测试与优雅停机使用）。"""
        deadline = time.monotonic() + timeout
        while True:
            if self._queue.unfinished_tasks == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    # -- subscription registry ---------------------------------------------
    def register(
        self,
        *,
        world_id: str,
        target_url: str,
        filter_spec: WebhookFilter | dict[str, Any] | None = None,
        secret: str = "",
        description: str = "",
        sub_id: str | None = None,
        enabled: bool = True,
        created_by: str = "",
    ) -> WebhookSubscription:
        clean_url = validate_webhook_url(target_url)
        clean_id = (sub_id or f"wh-{uuid.uuid4().hex[:10]}").strip()
        if not clean_id:
            raise ValueError("sub_id must not be empty")
        wf = (
            filter_spec
            if isinstance(filter_spec, WebhookFilter)
            else WebhookFilter.from_mapping(filter_spec)
        )
        sub = WebhookSubscription(
            sub_id=clean_id,
            world_id=world_id,
            target_url=clean_url,
            filter=wf,
            secret=str(secret or ""),
            description=str(description or ""),
            enabled=bool(enabled),
            created_by=str(created_by or ""),
        )
        with self._lock:
            self._subscriptions[clean_id] = sub
            self._deliveries.setdefault(
                clean_id, deque(maxlen=self._max_delivery_history)
            )
        return sub

    def list_subscriptions(self, world_id: str | None = None) -> list[WebhookSubscription]:
        with self._lock:
            subs = list(self._subscriptions.values())
        if world_id is None:
            return subs
        return [s for s in subs if s.world_id == world_id]

    def get_subscription(self, sub_id: str) -> WebhookSubscription:
        with self._lock:
            sub = self._subscriptions.get(sub_id)
        if sub is None:
            raise KeyError(f"unknown webhook subscription: {sub_id!r}")
        return sub

    def delete_subscription(
        self, sub_id: str, *, world_id: str | None = None
    ) -> WebhookSubscription:
        with self._lock:
            sub = self._subscriptions.get(sub_id)
            if sub is None or (world_id is not None and sub.world_id != world_id):
                raise KeyError(f"unknown webhook subscription: {sub_id!r}")
            del self._subscriptions[sub_id]
        return sub

    def list_deliveries(
        self, sub_id: str, *, world_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            sub = self._subscriptions.get(sub_id)
            if sub is None or (world_id is not None and sub.world_id != world_id):
                raise KeyError(f"unknown webhook subscription: {sub_id!r}")
            rows = list(self._deliveries.get(sub_id, ()))
        return [row.to_dict() for row in rows]

    # -- hook / dispatch ---------------------------------------------------
    def make_post_commit_hook(
        self, world_id: str
    ) -> Callable[[Actor, dict[str, Any]], None]:
        """为指定 ``world_id`` 生成幂等稳定的 post-commit 回调。"""

        def _hook(actor: Actor, run: dict[str, Any]) -> None:
            self.on_post_commit(world_id, actor, run)

        setattr(_hook, "_webhook_world_id", world_id)
        return _hook

    def on_post_commit(
        self, world_id: str, actor: Actor, run: dict[str, Any]
    ) -> list[str]:
        """在事务已提交后调用：同步筛选订阅并异步入队；永不向业务事务抛异常。"""
        if str(run.get("status") or "") != "COMMITTED":
            return []

        with self._lock:
            if self._closed:
                return []
            candidates = [
                sub
                for sub in self._subscriptions.values()
                if sub.enabled and sub.world_id == world_id
            ]

        enqueued: list[str] = []
        for sub in candidates:
            changes = sub.filter.match_run(run)
            if changes is None:
                continue
            delivery_id = f"dlv-{uuid.uuid4().hex[:12]}"
            envelope = build_event_envelope(
                world_id=world_id, actor=actor, run=run, changes=changes
            )
            task = _DeliveryTask(
                subscription=sub, delivery_id=delivery_id, envelope=envelope
            )
            try:
                self._queue.put_nowait(task)
                enqueued.append(delivery_id)
            except queue.Full:
                self._record_delivery(
                    WebhookDelivery(
                        delivery_id=delivery_id,
                        sub_id=sub.sub_id,
                        event_id=str(envelope["event_id"]),
                        world_id=world_id,
                        target_url=sub.target_url,
                        status="FAILED",
                        attempts=0,
                        error="webhook queue full",
                    )
                )
        return enqueued

    def _record_delivery(self, delivery: WebhookDelivery) -> None:
        with self._lock:
            buf = self._deliveries.setdefault(
                delivery.sub_id, deque(maxlen=self._max_delivery_history)
            )
            buf.append(delivery)

    def _run_worker(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                self._deliver_with_retry(task)
            except Exception:  # pragma: no cover - defensive worker guard
                pass
            finally:
                self._queue.task_done()

    def _deliver_with_retry(self, task: _DeliveryTask) -> None:
        sub = task.subscription
        body = json.dumps(task.envelope, ensure_ascii=False, default=str).encode(
            "utf-8"
        )
        headers: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Ontology-Event": str(task.envelope["event_type"]),
            "X-Ontology-World": sub.world_id,
            "X-Ontology-Delivery": task.delivery_id,
        }
        if sub.secret:
            headers["X-Ontology-Signature-256"] = sign_payload(sub.secret, body)

        with self._lock:
            sender = self._sender

        last_status: int | None = None
        failure_detail = ""
        attempts = 0
        max_attempts = 1 + self._max_retries

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                status_code, detail = sender(
                    sub.target_url, dict(headers), body, self._timeout
                )
                last_status = int(status_code)
                if 200 <= last_status < 300:
                    self._record_delivery(
                        WebhookDelivery(
                            delivery_id=task.delivery_id,
                            sub_id=sub.sub_id,
                            event_id=str(task.envelope["event_id"]),
                            world_id=sub.world_id,
                            target_url=sub.target_url,
                            status="DELIVERED",
                            attempts=attempts,
                            http_status=last_status,
                        )
                    )
                    return
                failure_detail = f"HTTP {last_status}: {detail[:240]}"
            except Exception as exc:
                failure_detail = f"{type(exc).__name__}: {exc}"

            if attempt < max_attempts and self._backoff_seconds > 0:
                time.sleep(self._backoff_seconds * (2 ** (attempt - 1)))

        self._record_delivery(
            WebhookDelivery(
                delivery_id=task.delivery_id,
                sub_id=sub.sub_id,
                event_id=str(task.envelope["event_id"]),
                world_id=sub.world_id,
                target_url=sub.target_url,
                status="FAILED",
                attempts=attempts,
                http_status=last_status,
                error=failure_detail,
            )
        )
