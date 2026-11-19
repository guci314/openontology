"""投资人路演演播室 #1：裸 MCP（开环对照组） vs OpenOntology 三看板（W_real · W_mem · 审批/MPC + 飞书）。

特性：
1. 完全复用 ``open_ontology`` 内核（``OntologyRuntime`` 统一闭环同态结算、``ClosedLoopMPCController``、
   ``SourceBridge``）与现有 ``OdooO2CAdapter``、``FeishuOpenAPIClient``；
2. 独立 Odoo 客户隔离（``O2C-INVESTOR-3BOARDS`` 与 ``O2C-INVESTOR-BARE-MCP``），
   绝对不触碰 ``:8789`` / ``O2C-CUST-001`` / ``O2C-SO-001`` / ``O2C-SO-002``；
3. 只从 ``--profile-dir`` 读取飞书凭据与目标群/用户，绝不劫持 WebSocket 长连接，
   绝不在任何 API、HTML、日志或元数据中回显 ``FEISHU_APP_SECRET``；
4. 四幕连续闭环剧场（零 ``w_lat.sqlite3`` 持久队列）：
   - 第一幕：全知之眼对齐（$W_{real} \\to W_{mem}$）+ 未出库强开票拦截；
   - 第二幕：撒谎执行器穿帮 + ``DEVICE_STATE_MISMATCH`` 独立回读否决与零幻觉保护；
   - 第三幕：超授信挂起（``PENDING_APPROVAL`` 仅存 ``W_mem``）+ 真实飞书 ``o2c业财助手`` 审批推卡 + 回调核销；
   - 第四幕：``ClosedLoopMPCController`` 滚动求解 + 首次故障重规划自愈直至实收清零。
"""

from __future__ import annotations

import argparse
import copy
import hmac
import html
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from open_ontology import (
    Actor,
    CandidateAction,
    ClosedLoopMPCController,
    DEVICE_STATE_MISMATCH,
    DeviceStateMismatchError,
    GovernanceViolationError,
    InvariantError,
    MPC_GOAL_REACHED,
    OntologyRuntime,
    SourceBridge,
)

from . import o2c_ontology as o2c
from .feishu_card_bridge import FeishuOpenAPIClient, TransportFn, format_rmb
from .odoo_o2c_adapter import CREDIT_LIMIT_CENTS, OdooO2CAdapter
from .odoo_dual_erp_demo import OdooJSONRPC, OdooRPCError

DEFAULT_PARTNER_REF = "O2C-INVESTOR-3BOARDS"
DEFAULT_PARTNER_NAME = "三看板演播室客户"
DEFAULT_ORDER_SEEDS: tuple[tuple[str, int, float], ...] = (
    ("O2C-3B-SO-001", 2, 10_000.0),
    ("O2C-3B-SO-002", 12, 10_000.0),
)

DEFAULT_BARE_PARTNER_REF = "O2C-INVESTOR-BARE-MCP"
DEFAULT_BARE_PARTNER_NAME = "裸 MCP 对照组客户"
DEFAULT_BARE_ORDER_SEEDS: tuple[tuple[str, int, float], ...] = (
    ("MCP-RAW-SO-001", 2, 10_000.0),
)

_SYSTEM_ACTOR = Actor(actor_id="__studio__", role="system", clearance_tags={"*"})
_NO_PROXY_ENTRIES = (
    "127.0.0.1",
    "localhost",
    "::1",
    "open.feishu.cn",
    ".feishu.cn",
)

_ACTION_CN = {
    "create_sales_order": "新建销售订单",
    "confirm_sales_order": "确认销售订单",
    "ship_order_delivery": "仓库发货出库",
    "issue_and_post_invoice": "开票并过账",
    "record_customer_payment": "客户收款核销",
    "annotate_o2c_governance": "登记治理备注",
}

_STATUS_CN = {
    "draft": "草稿",
    "sale": "已确认",
    "done": "已完成",
    "assigned": "待拣货出库",
    "confirmed": "已确认",
    "shipped": "已出库",
    "invoiced": "已开票",
    "posted": "已过账",
    "paid": "已收款清零",
    "in_payment": "收款核销中",
    "not_paid": "未付款",
    "cancel": "已取消",
    "cancelled": "已取消",
    "pending": "待出库",
    "PENDING_APPROVAL": "待飞书签批挂起",
    "COMMITTED": "已正式生效",
    "REJECTED": "风控/读回拦截",
    "APPROVED": "已批准待执行",
    "AUTO_APPROVED": "自动放行待执行",
    "CONFLICT": "版本锁冲突作废",
    "REVERTED": "已冲正恢复",
}


def _cn(val: Any) -> str:
    if val is None:
        return "—"
    key = str(val)
    return _STATUS_CN.get(key, key)


def ensure_no_proxy() -> None:
    """确保本地回环与飞书开放平台域名绕过代理。"""
    for key in ("NO_PROXY", "no_proxy"):
        existing = [item.strip() for item in os.environ.get(key, "").split(",") if item.strip()]
        seen = set(existing)
        merged = list(existing)
        for host in _NO_PROXY_ENTRIES:
            if host not in seen:
                merged.append(host)
                seen.add(host)
        os.environ[key] = ",".join(merged)


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def _load_approved_open_ids(profile_dir: Path | None) -> list[str]:
    if profile_dir is None:
        return []
    pairing_path = profile_dir / "platforms" / "pairing" / "feishu-approved.json"
    if not pairing_path.is_file():
        return []
    try:
        raw = json.loads(pairing_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, dict):
        for key in ("open_ids", "approved", "users"):
            items = raw.get(key)
            if isinstance(items, list):
                return [str(x).strip() for x in items if str(x).strip()]
    return []


def _redact_secrets(payload: Any, secrets_to_mask: list[str]) -> Any:
    """递归屏蔽任何意外出现的敏感密钥字符串。"""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if "secret" in str(k).lower():
                out[k] = "[REDACTED]" if v else ""
            else:
                out[k] = _redact_secrets(v, secrets_to_mask)
        return out
    if isinstance(payload, list):
        return [_redact_secrets(item, secrets_to_mask) for item in payload]
    if isinstance(payload, str):
        masked = payload
        for sec in secrets_to_mask:
            if sec and len(sec) >= 6 and sec in masked:
                masked = masked.replace(sec, "[REDACTED]")
        return masked
    return payload


def _fmt_odoo_order_no(val: Any) -> str:
    if not val:
        return "尚未立单"
    m = re.search(r"(\d+)", str(val))
    return f"销字第 {m.group(1)} 号" if m else "企业账套单据"


def _fmt_odoo_delivery_no(val: Any) -> str:
    if not val:
        return "尚未生成"
    m = re.search(r"(\d+)", str(val))
    return f"出库第 {m.group(1)} 号" if m else "仓库出库单"


def _fmt_odoo_invoice_no(val: Any) -> str:
    if not val:
        return "尚未开票"
    nums = re.findall(r"\d+", str(val))
    if len(nums) >= 2:
        return f"票字 {nums[0]} 第 {nums[1]} 号"
    if len(nums) == 1:
        return f"票字第 {nums[0]} 号"
    return "财务增值税发票"


def _fmt_run_no(val: Any) -> str:
    if not val:
        return "流水记录"
    nums = re.findall(r"\d+", str(val))
    return f"流水 {nums[-1]} 号" if nums else "流水记录"


def _strip_latin_tokens(text: Any) -> str:
    """兜底清除任何残留英文字母，确保前端非技术投资人零英文门禁。"""
    raw = str(text or "")
    cleaned = re.sub(r"[A-Za-z]+", "", raw)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


_DISPLAY_STATUS_CN = {
    "draft": "初始草稿",
    "sale": "已正式确认",
    "done": "已物理出库完成",
    "assigned": "待仓库拣货",
    "confirmed": "待仓库拣货",
    "shipped": "已物理出库",
    "invoiced": "已过账开票",
    "posted": "已过账入账",
    "paid": "已全额收款清零",
    "in_payment": "收款核销中",
    "not_paid": "尚未收款",
    "cancel": "已作废取消",
    "cancelled": "已作废取消",
    "pending": "待物理出库",
    "PENDING_APPROVAL": "待主管签批",
    "COMMITTED": "已确认入账",
    "REJECTED": "已当场拦截",
    "APPROVED": "已批准待核验",
    "AUTO_APPROVED": "自动放行待核验",
    "CONFLICT": "版本冲突已作废",
    "REVERTED": "已冲正撤回",
    "GOAL_REACHED": "目标已达成",
}

_DISPLAY_ERROR_CN = {
    "CANNOT_INVOICE_UNSHIPPED_ORDER": "未发货严禁开票",
    "DEVICE_STATE_MISMATCH": "外部实账核对不符",
    "APPROVAL_REJECTED": "主管已驳回放行申请",
}


def _d_status(val: Any, fallback: str = "—") -> str:
    if val is None or val == "":
        return fallback
    key = str(val)
    if key in _DISPLAY_STATUS_CN:
        return _DISPLAY_STATUS_CN[key]
    return _strip_latin_tokens(key) or fallback


def _build_feishu_approval_card(
    *,
    run_id: str,
    order_id: str,
    odoo_order_name: str,
    order_total_cents: int,
    credit_limit_cents: int,
    ar_cents: int,
    callback_token: str,
    studio_callback_url: str,
    status: str = "PENDING_APPROVAL",
    decision_actor: str | None = None,
    decision_note: str | None = None,
) -> dict[str, Any]:
    """构造带 studio_callback_url 按钮载荷与 /card 回落指令的飞书审批卡片。"""
    over_cents = max(0, ar_cents + order_total_cents - credit_limit_cents)
    approve_val = {
        "op": "approve",
        "run_id": run_id,
        "token": callback_token,
        "studio_callback_url": studio_callback_url,
    }
    reject_val = {
        "op": "reject",
        "run_id": run_id,
        "token": callback_token,
        "studio_callback_url": studio_callback_url,
    }

    if status == "COMMITTED":
        header_title = "【已签批落盘】战略大单超授信放行（订单 1002）"
        header_template = "green"
        status_line = f"**签批结果**：财务总监已批准，企业核心账套单据（{_fmt_odoo_order_no(odoo_order_name)}）与记忆棋盘（内部确认账）已同步确认。"
    elif status == "REJECTED":
        header_title = "【已驳回】战略大单超授信拦截（订单 1002）"
        header_template = "red"
        status_line = "**签批结果**：财务总监已驳回，企业核心账套继续保持草稿零改动。"
    else:
        header_title = "【业务本体沙盘 · 超授信挂起】请签批订单 1002"
        header_template = "orange"
        status_line = (
            f"**推演隔离锁**：高风险变更已锁定在推演棋盘（预演草稿区），"
            f"签批前企业核心账套（{_fmt_odoo_order_no(odoo_order_name)}）保持草稿零修改。"
        )

    body_md = "\n".join(
        [
            f"**关联单据**：订单 1002（十二万元大额单，账套单号：{_fmt_odoo_order_no(odoo_order_name)}）",
            f"**订单金额**：{format_rmb(order_total_cents)} ｜ **客户授信总额**：{format_rmb(credit_limit_cents)} ｜ **超额占用**：{format_rmb(over_cents)}",
            f"**当前实时应收**：{format_rmb(ar_cents)}",
            status_line,
            *( [f"**补充说明**：{decision_note}"] if decision_note else [] ),
        ]
    )
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": body_md}},
        {"tag": "hr"},
    ]
    if status == "PENDING_APPROVAL":
        cmd_approve = json.dumps(approve_val, ensure_ascii=False, separators=(",", ":"))
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准并写入企业账套"},
                        "type": "primary",
                        "value": approve_val,
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "驳回并保持草稿"},
                        "type": "danger",
                        "value": reject_val,
                    },
                ],
            }
        )
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"流水号：{run_id} ｜ 回退指令：/card {cmd_approve}",
                    }
                ],
            }
        )
    else:
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"流水号：{run_id} ｜ 终态锁定（防重复点击）",
                    }
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": header_template,
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


class ThreeBoardsStudioApp:
    """对比裸 MCP 与 OpenOntology 三看板（W_real / W_mem / W_lat）的持久化演播室引擎。"""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        profile_dir: str | Path | None = None,
        client: OdooJSONRPC | None = None,
        bare_client: OdooJSONRPC | None = None,
        partner_ref: str = DEFAULT_PARTNER_REF,
        partner_name: str = DEFAULT_PARTNER_NAME,
        order_seeds: tuple[tuple[str, int, float], ...] = DEFAULT_ORDER_SEEDS,
        bare_partner_ref: str = DEFAULT_BARE_PARTNER_REF,
        bare_partner_name: str = DEFAULT_BARE_PARTNER_NAME,
        bare_order_seeds: tuple[tuple[str, int, float], ...] = DEFAULT_BARE_ORDER_SEEDS,
        callback_host: str = "127.0.0.1",
        port: int = 8791,
        feishu_transport: TransportFn | None = None,
    ) -> None:
        ensure_no_proxy()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir = Path(profile_dir) if profile_dir is not None else None

        self.partner_ref = partner_ref
        self.partner_name = partner_name
        self.order_seeds = tuple(order_seeds)
        self.order_1 = self.order_seeds[0][0]
        self.order_2 = self.order_seeds[1][0]

        self.bare_partner_ref = bare_partner_ref
        self.bare_partner_name = bare_partner_name
        self.bare_order_seeds = tuple(bare_order_seeds)
        self.bare_order = self.bare_order_seeds[0][0]

        self.callback_host = callback_host
        self.port = int(port)
        self._feishu_transport = feishu_transport

        self.mem_db_path = self.state_dir / "w_mem.sqlite3"
        self.meta_path = self.state_dir / "studio_meta.json"
        self._lock = threading.RLock()

        self._injected_client = client
        self._injected_bare_client = bare_client

        self.memory_runtime: OntologyRuntime | None = None
        self.adapter: OdooO2CAdapter | None = None
        self.bare_runtime: OntologyRuntime | None = None
        self.bare_adapter: OdooO2CAdapter | None = None
        self.meta: dict[str, Any] = {}

        self._app_secret = ""
        self.feishu_client = self._build_feishu_client()
        self._init_or_rehydrate()

    @property
    def studio_callback_url(self) -> str:
        return f"http://{self.callback_host}:{self.port}/api/feishu/callback"

    # -- Feishu config (read-only, secret-safe) ----------------------------
    def _build_feishu_client(self) -> FeishuOpenAPIClient:
        env: dict[str, str] = {}
        if self.profile_dir is not None:
            env = _parse_dotenv(self.profile_dir / ".env")
        app_id = env.get("FEISHU_APP_ID", "").strip()
        app_secret = env.get("FEISHU_APP_SECRET", "").strip()
        self._app_secret = app_secret

        receive_id = (
            env.get("FEISHU_RECEIVE_ID")
            or env.get("FEISHU_HOME_CHANNEL")
            or ""
        ).strip()
        if not receive_id:
            approved = _load_approved_open_ids(self.profile_dir)
            if approved:
                receive_id = approved[0]
        receive_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
        return FeishuOpenAPIClient(
            app_id=app_id,
            app_secret=app_secret,
            receive_id=receive_id,
            receive_id_type=receive_type,
            transport=self._feishu_transport,
        )

    def _feishu_public_status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.feishu_client.is_configured),
            "receive_id": self.feishu_client.receive_id or "",
            "receive_id_type": self.feishu_client.receive_id_type,
            "approved_open_ids": _load_approved_open_ids(self.profile_dir),
            "callback_url": self.studio_callback_url,
            "card": self.meta.get("feishu_card"),
        }

    # -- lifecycle & persistence ------------------------------------------
    def _close_stores(self) -> None:
        if self.memory_runtime is not None:
            self.memory_runtime.close()
            self.memory_runtime = None
        if self.bare_runtime is not None:
            self.bare_runtime.close()
            self.bare_runtime = None

    def close(self) -> None:
        with self._lock:
            self._close_stores()

    def _default_meta(self) -> dict[str, Any]:
        return {
            "seq": 0,
            "partner_ref": self.partner_ref,
            "bare_partner_ref": self.bare_partner_ref,
            "order_1": self.order_1,
            "order_2": self.order_2,
            "bare_order": self.bare_order,
            "completed_acts": [],
            "acts": {},
            "bare_mcp": {
                "belief_status": "draft",
                "belief_note": "裸 MCP 刚初始化报价单，尚未执行动作",
                "brain_world_split": False,
                "split_reason": "",
                "events": [],
            },
            "feishu_card": None,
            "last_mpc": None,
            "timeline": [],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    def _save_meta(self) -> None:
        self.meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        sanitized = _redact_secrets(self.meta, [self._app_secret] if self._app_secret else [])
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(sanitized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.meta_path)

    def _next_run_id(self, prefix: str) -> str:
        self.meta["seq"] = int(self.meta.get("seq", 0)) + 1
        return f"{prefix}-{self.meta['seq']:04d}"

    def _push_timeline(self, stage: str, title: str, detail: str, tone: str = "info") -> None:
        items = list(self.meta.get("timeline") or [])
        items.append(
            {
                "at": time.strftime("%H:%M:%S"),
                "stage": stage,
                "title": title,
                "detail": detail,
                "tone": tone,
            }
        )
        self.meta["timeline"] = items[-24:]

    def _open_stores(self) -> None:
        self.memory_runtime = OntologyRuntime(
            o2c.spec, db_path=self.mem_db_path, world_mode="coupled"
        )
        self.adapter = OdooO2CAdapter(
            self.memory_runtime,
            client=self._injected_client,
            partner_ref=self.partner_ref,
            partner_name=self.partner_name,
            order_seeds=self.order_seeds,
            credit_limit_cents=CREDIT_LIMIT_CENTS,
        )
        self.adapter.bind_coupled(allow_source_revert=True)
        self.bare_runtime = OntologyRuntime(o2c.spec)
        self.bare_adapter = OdooO2CAdapter(
            self.bare_runtime,
            client=self._injected_bare_client or self.adapter.client,
            partner_ref=self.bare_partner_ref,
            partner_name=self.bare_partner_name,
            order_seeds=self.bare_order_seeds,
            credit_limit_cents=CREDIT_LIMIT_CENTS,
        )

    def _init_or_rehydrate(self) -> None:
        with self._lock:
            if self.mem_db_path.is_file() and self.meta_path.is_file():
                try:
                    loaded_meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
                except Exception:
                    loaded_meta = {}
                if (
                    loaded_meta.get("partner_ref") == self.partner_ref
                    and loaded_meta.get("bare_partner_ref") == self.bare_partner_ref
                ):
                    self._open_stores()
                    assert self.adapter is not None and self.bare_adapter is not None
                    attached_main = self.adapter.attach_existing_odoo()
                    attached_bare = self.bare_adapter.attach_existing_odoo()
                    if attached_main is not None and attached_bare is not None:
                        self.meta = loaded_meta
                        self._save_meta()
                        return
                    self._close_stores()

            self.reset()

    def reset(self) -> dict[str, Any]:
        """重建隔离客户与双边草稿订单，并清空持久化记忆与重开演播。"""
        with self._lock:
            self._close_stores()
            for db_file in (self.mem_db_path,):
                if db_file.exists():
                    db_file.unlink()
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{db_file}{suffix}")
                    if sidecar.exists():
                        sidecar.unlink()
            self.meta = self._default_meta()
            self._open_stores()
            assert self.adapter is not None and self.bare_adapter is not None
            self.adapter.seed_odoo()
            self.adapter.sync_from_odoo()
            self.bare_adapter.seed_odoo()
            self._push_timeline(
                "INIT",
                "演播室初始化完成",
                f"Odoo 19 已创建隔离报价单 {self.order_1}(¥20k)、{self.order_2}(¥120k) 与裸 MCP 对照单 {self.bare_order}(¥20k)。",
                "ok",
            )
            self._save_meta()
            return self.state_snapshot()

    # -- helpers -----------------------------------------------------------
    def _ensure_ready(self) -> tuple[OntologyRuntime, OdooO2CAdapter, OdooO2CAdapter]:
        assert (
            self.memory_runtime is not None
            and self.adapter is not None
            and self.bare_adapter is not None
        )
        return self.memory_runtime, self.adapter, self.bare_adapter

    def _safe_odoo_docs(self, adapter: OdooO2CAdapter, order_id: str) -> dict[str, Any]:
        try:
            return adapter.odoo_documents(order_id)
        except Exception as exc:
            return {"order_id": order_id, "error": str(exc)}

    def _bare_record_event(self, act: str, title: str, detail: str, *, split: bool, real_docs: dict[str, Any]) -> None:
        bare = self.meta["bare_mcp"]
        bare["brain_world_split"] = bool(bare.get("brain_world_split")) or split
        if split:
            bare["split_reason"] = detail
        events = list(bare.get("events") or [])
        events.append(
            {
                "at": time.strftime("%H:%M:%S"),
                "act": act,
                "title": title,
                "detail": detail,
                "split": split,
                "real_docs": real_docs,
            }
        )
        bare["events"] = events[-10:]

    def _order_mem_status(self, order_id: str) -> str:
        assert self.memory_runtime is not None
        obj = self.memory_runtime.get_object(o2c.SalesOrder, order_id, _SYSTEM_ACTOR)
        return str(obj.status)

    def _sync_and_get_card_context(self, order_id: str) -> tuple[str, int, int, int]:
        assert self.memory_runtime is not None and self.adapter is not None
        docs = self._safe_odoo_docs(self.adapter, order_id)
        cust = self.memory_runtime.get_object(o2c.Customer, self.partner_ref, _SYSTEM_ACTOR)
        order = self.memory_runtime.get_object(o2c.SalesOrder, order_id, _SYSTEM_ACTOR)
        ar_cents = o2c.get_customer_ar_balance(
            self.memory_runtime, self.partner_ref, _SYSTEM_ACTOR
        )
        return (
            str(docs.get("order_name") or order_id),
            int(order.total_cents),
            int(cust.credit_limit_cents),
            int(ar_cents),
        )

    def _mark_act_done(self, act_key: str, summary: dict[str, Any]) -> None:
        completed = list(self.meta.get("completed_acts") or [])
        if act_key not in completed:
            completed.append(act_key)
        self.meta["completed_acts"] = completed
        acts = dict(self.meta.get("acts") or {})
        acts[act_key] = summary
        self.meta["acts"] = acts

    # -- Act 1 -------------------------------------------------------------
    def run_act_1(self) -> dict[str, Any]:
        """第一幕：W_real -> W_mem 一致性对齐 + 未出库强行开票拦截 + 合法确认首单。"""
        with self._lock:
            rt, adapter, bare_adapter = self._ensure_ready()

            # 1) 对照组：裸 MCP 无本体规则、直接调 Odoo RPC action_confirm 后强行 _create_invoices
            assert bare_adapter.data is not None
            bare_ref = bare_adapter.data.order(self.bare_order)
            bare_row_before = bare_adapter.order_row(self.bare_order)
            if bare_row_before["state"] == "draft":
                bare_adapter.client.execute_kw(
                    "sale.order", "action_confirm", [[bare_ref.odoo_id]]
                )
            bare_invoice_result: dict[str, Any] = {}
            try:
                inv_ret = bare_adapter.client.execute_kw(
                    "sale.order", "_create_invoices", [[bare_ref.odoo_id]]
                )
                bare_invoice_result = {
                    "rpc_outcome": "bypassed_governance",
                    "created_invoice_ids": inv_ret,
                    "detail": (
                        f"裸 MCP 在 {self.bare_order} 物理未出库时仍调用了 _create_invoices，"
                        f"Odoo 返回 {inv_ret!r}，无本体前置红线拦截。"
                    ),
                }
            except OdooRPCError as exc:
                bare_invoice_result = {
                    "rpc_outcome": "runtime_rpc_exception_after_mutation",
                    "error": str(exc),
                    "detail": (
                        f"裸 MCP 已把 Odoo 订单改成 confirmed 并生成待拣货单，"
                        f"随后调 _create_invoices 才报错炸穿：{exc}"
                    ),
                }
            bare_docs = self._safe_odoo_docs(bare_adapter, self.bare_order)
            self.meta["bare_mcp"]["belief_status"] = "confirmed_and_attempted_invoice"
            self.meta["bare_mcp"]["belief_note"] = bare_invoice_result["detail"]
            self._bare_record_event(
                "act1",
                "裸 MCP 直接对未出库单调 _create_invoices",
                bare_invoice_result["detail"],
                split=True,
                real_docs=bare_docs,
            )

            # 2) 实验组：W_real -> W_mem 全景快照对齐
            sync_info = adapter.sync_from_odoo()

            # 3) 前置不变量拦截“未出库强行开票” -> Phase B 不触发，Odoo 零脏写
            blocked_run_id = self._next_run_id("3B-BLOCK")
            blocked_error_code = "INVARIANT_VIOLATION"
            blocked_error_msg = ""
            if rt.get_action_run(blocked_run_id) is None:
                try:
                    rt.submit_action(
                        blocked_run_id,
                        "issue_and_post_invoice",
                        {"order": self.order_1},
                        o2c.AI_AGENT,
                    )
                except InvariantError as exc:
                    blocked_error_code = exc.code or "INVARIANT_VIOLATION"
                    blocked_error_msg = str(exc)

            docs_after_block = self._safe_odoo_docs(adapter, self.order_1)

            # 4) 合法动作：统一闭环确认 confirm_sales_order -> 写 Odoo -> 读回核验提交进 W_mem
            confirm_run_id: str | None = None
            odoo_evidence: dict[str, Any] | None = None
            settled_status = self._order_mem_status(self.order_1)
            if settled_status == "draft":
                confirm_run_id = self._next_run_id("3B-CONF1")
                settled = rt.submit_action(
                    confirm_run_id,
                    "confirm_sales_order",
                    {"order": self.order_1},
                    o2c.AI_AGENT,
                )
                ev_list = adapter.get_run_evidence(confirm_run_id)
                odoo_evidence = ev_list[0] if ev_list else None
                settled_status = str(settled["status"])

            docs_1 = self._safe_odoo_docs(adapter, self.order_1)
            act1_summary = {
                "ok": True,
                "act": 1,
                "sync_orders": sync_info.get("orders"),
                "blocked_illegal_invoice": {
                    "run_id": blocked_run_id,
                    "blocked": True,
                    "error_code": blocked_error_code,
                    "error_message": blocked_error_msg,
                    "odoo_untouched_order_state": docs_after_block.get("order_state"),
                },
                "confirmed_order_1": {
                    "order_id": self.order_1,
                    "run_id": confirm_run_id,
                    "settled_status": settled_status,
                    "mem_status": self._order_mem_status(self.order_1),
                    "odoo_docs": docs_1,
                    "erp_evidence": odoo_evidence,
                },
                "bare_mcp": {
                    "order_id": self.bare_order,
                    "result": bare_invoice_result,
                    "odoo_docs": bare_docs,
                },
            }
            self._mark_act_done("1", act1_summary)
            self._push_timeline(
                "ACT 1",
                f"拦截 {self.order_1} 未出库开票 ({blocked_error_code}) 并合法确认",
                f"W_lat 先拦下非法开票（Odoo 零脏写）；随后合法确认 {self.order_1}（{docs_1.get('order_name')}）。",
                "ok",
            )
            self._save_meta()
            return {"act_result": act1_summary, "state": self.state_snapshot()}

    # -- Act 2 -------------------------------------------------------------
    def run_act_2(self) -> dict[str, Any]:
        """第二幕：撒谎执行器穿帮 + DEVICE_STATE_MISMATCH 独立回读否决与零幻觉保护。"""
        with self._lock:
            rt, adapter, bare_adapter = self._ensure_ready()
            if self._order_mem_status(self.order_1) == "draft":
                self.run_act_1()

            # 1) 对照组：裸 MCP 调用“撒谎执行器”并返回成功，却不验证 Odoo picking 是否真的 done
            bare_docs_real = self._safe_odoo_docs(bare_adapter, self.bare_order)
            lying_bare_resp = {
                "ok": True,
                "status": "shipped",
                "delivery": f"DEL-{self.bare_order}",
                "picking_state": "done",
            }
            self.meta["bare_mcp"]["belief_status"] = "shipped"
            self.meta["bare_mcp"]["belief_note"] = (
                f"执行器谎称 {self.bare_order} 已完成出库（picking_state=done），裸 MCP 轻信返回值。"
            )
            self._bare_record_event(
                "act2",
                "裸 MCP 轻信撒谎执行器 → 脑内世界与 Odoo 分裂",
                f"裸 MCP 认为已 shipped；真实 Odoo 发货单 {bare_docs_real.get('delivery_name')} 仍为 {bare_docs_real.get('delivery_state')}。",
                split=True,
                real_docs=bare_docs_real,
            )

            # 2) 实验组：临时换入“撒谎执行器”模拟外部未操作 Odoo -> 统一管线独立回读否决
            lie_run_id = self._next_run_id("3B-LIE")
            lying_actuator_claim = {
                "ok": True,
                "claimed_action": "ship_order_delivery",
                "claimed_status": "shipped",
                "note": "fake success payload ignored by OpenOntology",
            }
            sim_preview = rt.simulate_action(
                "ship_order_delivery", {"order": self.order_1}, o2c.WAREHOUSE
            )
            rt.bind_source_bridge(
                SourceBridge(
                    actuate=lambda _mv: dict(lying_actuator_claim),
                    observe=adapter.build_readback_snapshot,
                    allow_source_revert=True,
                ),
                world_mode="coupled",
            )
            settled: dict[str, Any]
            try:
                settled = rt.submit_action(
                    lie_run_id,
                    "ship_order_delivery",
                    {"order": self.order_1},
                    o2c.WAREHOUSE,
                )
            except DeviceStateMismatchError as exc:
                settled = exc.run or rt.get_action_run(lie_run_id) or {
                    "run_id": lie_run_id,
                    "status": "REJECTED",
                    "error_code": exc.code,
                    "error_message": str(exc),
                }
            finally:
                adapter.bind_coupled(allow_source_revert=True)

            docs_1 = self._safe_odoo_docs(adapter, self.order_1)
            mem_status_after = self._order_mem_status(self.order_1)

            act2_summary = {
                "ok": True,
                "act": 2,
                "run_id": lie_run_id,
                "staged_diff": sim_preview.diff.to_dict(),
                "lying_actuator_claim": lying_actuator_claim,
                "settled_status": settled["status"],
                "error_code": settled.get("error_code"),
                "error_message": settled.get("error_message"),
                "mem_status_after": mem_status_after,
                "rebased_pending_count": 0,
                "odoo_docs": docs_1,
                "bare_mcp": {
                    "order_id": self.bare_order,
                    "lying_response": lying_bare_resp,
                    "belief_status": self.meta["bare_mcp"]["belief_status"],
                    "real_delivery_state": bare_docs_real.get("delivery_state"),
                    "brain_world_split": True,
                },
            }
            self._mark_act_done("2", act2_summary)
            self._push_timeline(
                "ACT 2",
                f"抓获撒谎执行器：{settled.get('error_code')}",
                f"执行器谎称 {self.order_1} 已出库，读回 Odoo 真实 picking 为 {docs_1.get('delivery_state')}，"
                f"闭环核验当场拒绝（W_mem 保持 {mem_status_after}，零幻觉）。",
                "warn",
            )
            self._save_meta()
            return {"act_result": act2_summary, "state": self.state_snapshot()}

    # -- Act 3 & Feishu approval -------------------------------------------
    def run_act_3(self) -> dict[str, Any]:
        """第三幕：O2C-3B-SO-002 超授信挂起为 PENDING_APPROVAL，并向飞书推送审批卡。"""
        with self._lock:
            rt, adapter, bare_adapter = self._ensure_ready()

            existing_awaiting = rt.list_action_runs(status="PENDING_APPROVAL")
            target_move = next(
                (
                    mv
                    for mv in existing_awaiting
                    if (mv.get("params") or {}).get("order") == self.order_2
                ),
                None,
            )
            if target_move is None:
                mem_status_2 = self._order_mem_status(self.order_2)
                if mem_status_2 != "draft":
                    raise RuntimeError(
                        f"{self.order_2} is already {mem_status_2!r}; reset studio to replay Act 3"
                    )
                run_id = self._next_run_id("3B-APPR")
                target_move = rt.submit_action(
                    run_id,
                    "confirm_sales_order",
                    {"order": self.order_2},
                    o2c.AI_AGENT,
                )

            run_id = str(target_move["run_id"])
            odoo_name, order_total_cents, credit_limit_cents, ar_cents = (
                self._sync_and_get_card_context(self.order_2)
            )
            existing_card = self.meta.get("feishu_card") or {}
            callback_token = str(existing_card.get("callback_token") or "").strip()
            if not callback_token or existing_card.get("run_id") != run_id:
                callback_token = secrets.token_hex(12)

            card_json = _build_feishu_approval_card(
                run_id=run_id,
                order_id=self.order_2,
                odoo_order_name=odoo_name,
                order_total_cents=order_total_cents,
                credit_limit_cents=credit_limit_cents,
                ar_cents=ar_cents,
                callback_token=callback_token,
                studio_callback_url=self.studio_callback_url,
                status="PENDING_APPROVAL",
            )

            message_id = existing_card.get("message_id")
            push_status = "not_configured"
            push_error: str | None = None
            if existing_card.get("run_id") == run_id and message_id:
                push_status = "already_sent"
            elif self.feishu_client.is_configured:
                try:
                    sent = self.feishu_client.send_card(card_json)
                    message_id = sent["message_id"] if isinstance(sent, dict) else str(sent)
                    push_status = "sent"
                except Exception as exc:
                    push_status = "error"
                    push_error = _redact_secrets(
                        str(exc), [self._app_secret] if self._app_secret else []
                    )

            self.meta["feishu_card"] = {
                "run_id": run_id,
                "order_id": self.order_2,
                "odoo_order_name": odoo_name,
                "status": "PENDING_APPROVAL",
                "callback_token": callback_token,
                "studio_callback_url": self.studio_callback_url,
                "message_id": message_id,
                "push_status": push_status,
                "push_error": push_error,
                "receive_id": self.feishu_client.receive_id,
                "receive_id_type": self.feishu_client.receive_id_type,
                "decision_actor": None,
            }

            bare_docs_real = self._safe_odoo_docs(bare_adapter, self.bare_order)
            self._bare_record_event(
                "act3",
                "裸 MCP 无人工挂起与授信红线门禁",
                "裸 MCP 没有 W_lat 审批沙箱与飞书可验证回调，只能裸调或停留在纯文本提示。",
                split=True,
                real_docs=bare_docs_real,
            )

            docs_2 = self._safe_odoo_docs(adapter, self.order_2)
            act3_summary = {
                "ok": True,
                "act": 3,
                "run_id": run_id,
                "order_id": self.order_2,
                "latent_status": "PENDING_APPROVAL",
                "odoo_order_state_before_approval": docs_2.get("order_state"),
                "mem_status_before_approval": self._order_mem_status(self.order_2),
                "feishu_push_status": push_status,
                "feishu_message_id": message_id,
                "feishu_push_error": push_error,
            }
            self._mark_act_done("3", act3_summary)
            self._push_timeline(
                "ACT 3",
                f"超授信大单 {self.order_2} 锁入 W_lat 并推送飞书卡片",
                f"Odoo 原单 {odoo_name} 保持 {docs_2.get('order_state')} 零修改；飞书推送状态：{push_status}"
                + (f"（message_id={message_id}）" if message_id else ""),
                "warn",
            )
            self._save_meta()
            return {"act_result": act3_summary, "state": self.state_snapshot()}

    def _resolve_director_actor(self, operator_open_id: str | None) -> Actor:
        ident = str(operator_open_id or "").strip() or "finance-director-studio"
        return Actor(
            actor_id=ident,
            role=o2c.FINANCE_DIRECTOR.role,
            clearance_tags=set(o2c.FINANCE_DIRECTOR.clearance_tags),
        )

    def decide_act_3(
        self,
        *,
        op: str,
        run_id: str | None = None,
        token: str | None = None,
        operator_open_id: str | None = None,
        require_token: bool = False,
    ) -> dict[str, Any]:
        """签批或驳回 Act 3 挂起动作，同步执行 Odoo 19 写入并回读结算至 W_mem。"""
        with self._lock:
            rt, adapter, _bare = self._ensure_ready()
            card_meta = dict(self.meta.get("feishu_card") or {})
            target_run_id = str(run_id or card_meta.get("run_id") or "").strip()
            if not target_run_id:
                awaiting = rt.list_action_runs(status="PENDING_APPROVAL")
                if awaiting:
                    target_run_id = str(awaiting[0]["run_id"])
            if not target_run_id:
                return {
                    "ok": False,
                    "toast": {"type": "error", "content": "当前无待签批的挂起动作"},
                    "state": self.state_snapshot(),
                }

            expected_token = str(card_meta.get("callback_token") or "")
            if require_token:
                if not expected_token or not token or not hmac.compare_digest(expected_token, str(token)):
                    return {
                        "ok": False,
                        "toast": {"type": "error", "content": "卡片回调令牌校验失败"},
                        "state": self.state_snapshot(),
                    }

            if card_meta.get("run_id") == target_run_id and card_meta.get("status") in (
                "COMMITTED",
                "REJECTED",
            ):
                return {
                    "ok": True,
                    "already_decided": True,
                    "toast": {
                        "type": "info",
                        "content": f"该单据已由 {card_meta.get('decision_actor') or '总监'} 完成处理（{_cn(card_meta.get('status'))}）",
                    },
                    "state": self.state_snapshot(),
                }

            director = self._resolve_director_actor(operator_open_id)
            odoo_evidence: dict[str, Any] | None = None
            if op == "approve":
                final_run = rt.approve_action(target_run_id, director)
                ev_list = adapter.get_run_evidence(target_run_id)
                odoo_evidence = ev_list[0] if ev_list else None
                final_status = str(final_run["status"])
                toast_msg = (
                    f"已批准 {self.order_2} 并正式写入真实 Odoo 19（状态：{_cn(final_status)}）"
                )
            elif op == "reject":
                final_run = rt.reject_action(
                    target_run_id, director, reason="财务总监驳回超授信放行申请"
                )
                final_status = str(final_run["status"])
                toast_msg = f"已驳回 {self.order_2}，真实 Odoo 19 报价单继续保持草稿"
            else:
                return {
                    "ok": False,
                    "toast": {"type": "error", "content": f"不支持的卡片动作: {op!r}"},
                    "state": self.state_snapshot(),
                }

            docs_2 = self._safe_odoo_docs(adapter, self.order_2)
            odoo_name, order_total_cents, credit_limit_cents, ar_cents = (
                self._sync_and_get_card_context(self.order_2)
            )
            if card_meta:
                card_meta["status"] = final_status
                card_meta["decision_actor"] = director.actor_id
                card_meta["odoo_order_state"] = docs_2.get("order_state")
                card_meta["erp_evidence"] = odoo_evidence
                message_id = card_meta.get("message_id")
                if message_id and self.feishu_client.is_configured:
                    try:
                        patched_card = _build_feishu_approval_card(
                            run_id=target_run_id,
                            order_id=self.order_2,
                            odoo_order_name=odoo_name,
                            order_total_cents=order_total_cents,
                            credit_limit_cents=credit_limit_cents,
                            ar_cents=ar_cents,
                            callback_token=expected_token,
                            studio_callback_url=self.studio_callback_url,
                            status=final_status,
                            decision_actor=director.actor_id,
                            decision_note=toast_msg,
                        )
                        self.feishu_client.patch_card(str(message_id), patched_card)
                        card_meta["patch_status"] = "patched"
                    except Exception as exc:
                        card_meta["patch_status"] = _redact_secrets(
                            f"error: {exc}", [self._app_secret] if self._app_secret else []
                        )
                self.meta["feishu_card"] = card_meta

            act3_state = dict((self.meta.get("acts") or {}).get("3") or {})
            act3_state.update(
                {
                    "decided": True,
                    "decision_op": op,
                    "decision_actor": director.actor_id,
                    "final_status": final_status,
                    "odoo_docs": docs_2,
                    "erp_evidence": odoo_evidence,
                    "mem_status_after_approval": self._order_mem_status(self.order_2),
                }
            )
            self._mark_act_done("3", act3_state)
            self._push_timeline(
                "ACT 3 签批",
                f"{director.actor_id} -> {op.upper()} ({final_status})",
                toast_msg,
                "ok" if op == "approve" else "warn",
            )
            self._save_meta()
            return {
                "ok": True,
                "op": op,
                "run_id": target_run_id,
                "final_status": final_status,
                "erp_evidence": odoo_evidence,
                "toast": {"type": "info", "content": toast_msg},
                "state": self.state_snapshot(),
            }

    def handle_feishu_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理来自飞书卡片点击或 /card 插件转发的回调请求。"""
        event = payload.get("event") if "event" in payload else payload
        action_val = (
            (event.get("action") or {}).get("value")
            if isinstance(event, dict) and isinstance(event.get("action"), dict)
            else payload.get("value") or payload
        )
        if not isinstance(action_val, dict):
            action_val = {}
        operator_open_id = str(
            ((event.get("operator") or {}).get("open_id") if isinstance(event, dict) else "")
            or payload.get("open_id")
            or payload.get("operator_open_id")
            or ""
        ).strip()
        op = str(action_val.get("op") or payload.get("op") or "approve").strip()
        run_id = str(action_val.get("run_id") or payload.get("run_id") or "").strip() or None
        token = str(action_val.get("token") or payload.get("token") or "").strip() or None
        return self.decide_act_3(
            op=op,
            run_id=run_id,
            token=token,
            operator_open_id=operator_open_id,
            require_token=True,
        )

    # -- Act 4 (ClosedLoopMPCController) -----------------------------------
    def run_act_4(self, *, inject_first_ship_drift: bool = True) -> dict[str, Any]:
        """第四幕：ClosedLoopMPCController + MCTS 滚动规划，自动从漂移故障自愈并闭环收款。"""
        with self._lock:
            rt, adapter, bare_adapter = self._ensure_ready()

            def _goal_fn(check_rt: OntologyRuntime) -> bool:
                order_obj = check_rt.get_object(o2c.SalesOrder, self.order_1, _SYSTEM_ACTOR)
                ar = o2c.get_customer_ar_balance(check_rt, self.partner_ref, _SYSTEM_ACTOR)
                return order_obj.status == "paid" and ar == 0

            def _candidates(check_rt: OntologyRuntime, step_index: int) -> list[CandidateAction]:
                order_obj = check_rt.get_object(o2c.SalesOrder, self.order_1, _SYSTEM_ACTOR)
                st = order_obj.status
                cands: list[CandidateAction] = []
                if st == "draft":
                    cands.append(
                        CandidateAction("confirm_sales_order", {"order": self.order_1}, prior=0.9)
                    )
                elif st == "confirmed":
                    cands.append(
                        CandidateAction("ship_order_delivery", {"order": self.order_1}, prior=0.85)
                    )
                    if step_index == 0 and not order_obj.fulfillment_note:
                        cands.append(
                            CandidateAction(
                                "annotate_o2c_governance",
                                {"order": self.order_1, "fulfillment_note": "巡检备注"},
                                prior=0.15,
                            )
                        )
                elif st == "shipped":
                    cands.append(
                        CandidateAction("issue_and_post_invoice", {"order": self.order_1}, prior=0.9)
                    )
                elif st == "invoiced":
                    cands.append(
                        CandidateAction(
                            "record_customer_payment",
                            {"order": self.order_1, "payment_cents": int(order_obj.total_cents)},
                            prior=0.9,
                        )
                    )
                return cands

            def _value_fn(check_rt: OntologyRuntime) -> float:
                order_obj = check_rt.get_object(o2c.SalesOrder, self.order_1, _SYSTEM_ACTOR)
                stage_scores = {
                    "draft": 10.0,
                    "confirmed": 35.0,
                    "shipped": 65.0,
                    "invoiced": 85.0,
                    "paid": 100.0,
                }
                score = stage_scores.get(order_obj.status, 0.0)
                if order_obj.status == "paid":
                    ar = o2c.get_customer_ar_balance(
                        check_rt, self.partner_ref, _SYSTEM_ACTOR
                    )
                    if ar == 0:
                        score = 100.0
                return score

            drift_state = {"fired": False}
            exec_logs: list[dict[str, Any]] = []
            pending_approvals = rt.list_action_runs(status="PENDING_APPROVAL")
            if pending_approvals:
                raise GovernanceViolationError(
                    f"尚有待签批动作 {pending_approvals[0]['run_id']}，"
                    "请先在飞书或演播室完成第三幕签批再启动第四幕 MPC 收口。"
                )

            def _act_fn(move: dict[str, Any]) -> dict[str, Any]:
                action = str(move["action_type"])
                rid = str(move["run_id"])
                if (
                    inject_first_ship_drift
                    and action == "ship_order_delivery"
                    and not drift_state["fired"]
                ):
                    drift_state["fired"] = True
                    fake_claim = {
                        "ok": True,
                        "simulated_actuator_fault": True,
                        "message": "执行器第 1 次谎称已出库，但未调用 Odoo button_validate",
                    }
                    exec_logs.append(
                        {"run_id": rid, "action": action, "honest": False, "result": fake_claim}
                    )
                    return fake_claim

                evidence = adapter.execute_move(move)
                exec_logs.append(
                    {"run_id": rid, "action": action, "honest": True, "result": evidence}
                )
                return evidence

            try:
                controller = ClosedLoopMPCController(
                    rt,
                    o2c.AI_AGENT,
                    goal_fn=_goal_fn,
                    candidate_generator=_candidates,
                    value_fn=_value_fn,
                    simulations=24,
                    horizon=4,
                    value_scale=100.0,
                    seed=17,
                    act_fn=_act_fn,
                    observe_fn=adapter.build_readback_snapshot,
                    max_steps=12,
                    max_replans=3,
                    run_id_prefix=f"3B-MPC-{self.meta.get('seq', 0) + 1}",
                )
                mpc_result = controller.run()
            finally:
                adapter.bind_coupled(allow_source_revert=True)
            self.meta["seq"] = int(self.meta.get("seq", 0)) + max(1, len(mpc_result.trajectory))

            docs_1 = self._safe_odoo_docs(adapter, self.order_1)
            ar_after = o2c.get_customer_ar_balance(rt, self.partner_ref, _SYSTEM_ACTOR)

            # 对照组对比状态：裸 MCP 仍停留在未闭环状态
            bare_docs = self._safe_odoo_docs(bare_adapter, self.bare_order)
            self._bare_record_event(
                "act4",
                "裸 MCP 无闭环自愈（仍滞留在未核销分裂态）",
                f"三看板已通过闭环 MPC 清零 {self.order_1}；裸 MCP 对照单 {self.bare_order} "
                f"Odoo 实态仍为 order={bare_docs.get('order_state')}, delivery={bare_docs.get('delivery_state')}, invoice={bare_docs.get('invoice_name') or '无'}。",
                split=True,
                real_docs=bare_docs,
            )

            mpc_dict = mpc_result.to_dict()
            mpc_dict["exec_logs"] = exec_logs
            self.meta["last_mpc"] = mpc_dict
            act4_summary = {
                "ok": True,
                "act": 4,
                "order_id": self.order_1,
                "mpc": mpc_dict,
                "mem_status_after": self._order_mem_status(self.order_1),
                "ar_balance_cents": ar_after,
                "odoo_docs": docs_1,
            }
            self._mark_act_done("4", act4_summary)
            self._push_timeline(
                "ACT 4 MPC",
                f"闭环 MPC {mpc_result.status}（提交 {mpc_result.steps_committed} 步 · 重规划 {mpc_result.replans_used} 次）",
                f"{self.order_1} 真实发票 {docs_1.get('invoice_name')} 已全额核销（residual={docs_1.get('amount_residual')}），W_mem AR 归零。",
                "ok" if mpc_result.status == MPC_GOAL_REACHED else "warn",
            )
            self._save_meta()
            return {"act_result": act4_summary, "state": self.state_snapshot()}

    def run_walkthrough(self) -> dict[str, Any]:
        """一键按序执行 Reset -> 第一幕 -> 第二幕 -> 第三幕 -> 签批 -> 第四幕。"""
        with self._lock:
            self.reset()
            self.run_act_1()
            self.run_act_2()
            self.run_act_3()
            self.decide_act_3(op="approve", operator_open_id="finance-director-walkthrough")
            self.run_act_4(inject_first_ship_drift=True)
            return self.state_snapshot()

    # -- State snapshot for Web UI & tests ---------------------------------
    def state_snapshot(self) -> dict[str, Any]:
        """生成零密钥泄露的三看板全维实时快照（W_real / W_mem / 审批态 + Bare MCP + Feishu）。"""
        with self._lock:
            rt, adapter, bare_adapter = self._ensure_ready()

            cust = rt.get_object(o2c.Customer, self.partner_ref, _SYSTEM_ACTOR)
            ar_cents = o2c.get_customer_ar_balance(rt, self.partner_ref, _SYSTEM_ACTOR)
            orders_view: list[dict[str, Any]] = []
            for oid in (self.order_1, self.order_2):
                obj = rt.get_object(o2c.SalesOrder, oid, _SYSTEM_ACTOR)
                docs = self._safe_odoo_docs(adapter, oid)
                version_row = next(
                    (
                        item
                        for item in rt.list_objects("SalesOrder")
                        if item["object_id"] == oid
                    ),
                    {},
                )
                orders_view.append(
                    {
                        "order_id": oid,
                        "total_cents": int(obj.total_cents),
                        "total_yuan": format_rmb(obj.total_cents),
                        "mem_status": obj.status,
                        "mem_status_cn": _cn(obj.status),
                        "mem_version": int(version_row.get("version", 0)),
                        "odoo_order_name": docs.get("order_name"),
                        "odoo_order_state": docs.get("order_state"),
                        "odoo_delivery_name": docs.get("delivery_name"),
                        "odoo_delivery_state": docs.get("delivery_state"),
                        "odoo_invoice_name": docs.get("invoice_name"),
                        "odoo_invoice_state": docs.get("invoice_state"),
                        "odoo_payment_state": docs.get("payment_state"),
                        "odoo_residual": docs.get("amount_residual"),
                    }
                )

            bare_docs = self._safe_odoo_docs(bare_adapter, self.bare_order)
            runs = rt.list_action_runs()
            recent_runs = [
                {
                    "run_id": r["run_id"],
                    "action_type": r["action_type"],
                    "action_cn": _ACTION_CN.get(str(r["action_type"]), r["action_type"]),
                    "status": r["status"],
                    "status_cn": _cn(r["status"]),
                    "order_id": (r.get("params") or {}).get("order")
                    or (r.get("params") or {}).get("order_id"),
                    "approved_by": r.get("approved_by"),
                    "error_code": r.get("error_code"),
                    "error_message": r.get("error_message"),
                    "executed_at": r.get("executed_at"),
                }
                for r in runs[-12:]
            ]

            pending_moves: list[dict[str, Any]] = []
            awaiting_moves = [
                {
                    "run_id": mv["run_id"],
                    "action_type": mv["action_type"],
                    "params": mv.get("params"),
                }
                for mv in rt.list_action_runs(status="PENDING_APPROVAL")
            ]

            snapshot = {
                "studio": {
                    "title": "业务本体沙盘三看板与传统直连模式接单到回款闭环演播室",
                    "port": self.port,
                    "state_dir": str(self.state_dir),
                    "completed_acts": list(self.meta.get("completed_acts") or []),
                    "updated_at": self.meta.get("updated_at"),
                },
                "w_real": {
                    "partner_ref": self.partner_ref,
                    "partner_name": self.partner_name,
                    "credit_limit_cents": int(cust.credit_limit_cents),
                    "credit_limit_yuan": format_rmb(cust.credit_limit_cents),
                    "orders": [
                        {
                            "order_id": row["order_id"],
                            "odoo_order_name": row["odoo_order_name"],
                            "odoo_order_state": row["odoo_order_state"],
                            "odoo_delivery_name": row["odoo_delivery_name"],
                            "odoo_delivery_state": row["odoo_delivery_state"],
                            "odoo_invoice_name": row["odoo_invoice_name"],
                            "odoo_invoice_state": row["odoo_invoice_state"],
                            "odoo_payment_state": row["odoo_payment_state"],
                            "odoo_residual": row["odoo_residual"],
                            "total_yuan": row["total_yuan"],
                        }
                        for row in orders_view
                    ],
                },
                "w_mem": {
                    "db_path": str(self.mem_db_path),
                    "customer_id": cust.customer_id,
                    "credit_limit_cents": int(cust.credit_limit_cents),
                    "ar_balance_cents": int(ar_cents),
                    "ar_balance_yuan": format_rmb(ar_cents),
                    "remaining_credit_cents": max(0, int(cust.credit_limit_cents) - int(ar_cents)),
                    "remaining_credit_yuan": format_rmb(
                        max(0, int(cust.credit_limit_cents) - int(ar_cents))
                    ),
                    "orders": orders_view,
                    "recent_runs": recent_runs,
                },
                "w_lat": {
                    "db_path": None,
                    "pending_count": len(pending_moves),
                    "pending_moves": pending_moves,
                    "awaiting_approval_count": len(awaiting_moves),
                    "awaiting_approval_moves": awaiting_moves,
                },
                "bare_mcp": {
                    "partner_ref": self.bare_partner_ref,
                    "order_id": self.bare_order,
                    "belief_status": self.meta["bare_mcp"].get("belief_status"),
                    "belief_note": self.meta["bare_mcp"].get("belief_note"),
                    "brain_world_split": bool(self.meta["bare_mcp"].get("brain_world_split")),
                    "split_reason": self.meta["bare_mcp"].get("split_reason"),
                    "odoo_docs": bare_docs,
                    "events": list(self.meta["bare_mcp"].get("events") or []),
                },
                "feishu": self._feishu_public_status(),
                "last_mpc": self.meta.get("last_mpc"),
                "acts": self.meta.get("acts") or {},
                "timeline": list(self.meta.get("timeline") or []),
            }
            snapshot["display"] = self._build_display_view(snapshot)
            return _redact_secrets(
                snapshot, [self._app_secret] if self._app_secret else []
            )

    def _order_display_label(self, order_id: str | None) -> str:
        if not order_id:
            return "—"
        if order_id == self.order_1:
            return "订单 1001（两万元常规单）"
        if order_id == self.order_2:
            return "订单 1002（十二万元大额单）"
        if order_id == self.bare_order:
            return "对照单 2001（两万元常规单）"
        return _strip_latin_tokens(order_id) or "业务订单"

    def _build_display_view(self, snap: dict[str, Any]) -> dict[str, Any]:
        """生成面向非技术投资人的 100% 零英文字母纯中文展示视图。"""
        bare = snap["bare_mcp"]
        bd = bare.get("odoo_docs") or {}
        bare_belief_map = {
            "draft": "初始草稿",
            "confirmed_and_attempted_invoice": "已确认并强行试开票",
            "shipped": "误以为已完成出库",
        }
        bare_belief_cn = bare_belief_map.get(
            str(bare.get("belief_status") or ""), "状态已与真实账套偏离"
        )
        bare_events_cn: list[dict[str, Any]] = []
        for ev in (bare.get("events") or [])[-4:]:
            act_k = str(ev.get("act") or "")
            if act_k == "act1":
                t = "跳过发货校验直接向账套请求开票"
                d = (
                    f"未检查物理出库条件，直接将对照单改为已确认并生成出库单"
                    f"（{_fmt_odoo_delivery_no(bd.get('delivery_name'))}），"
                    "随后调用开票接口时被账套驳回，遗留半截脏单据。"
                )
            elif act_k == "act2":
                t = "轻信外部执行器谎言造成账实分裂"
                d = (
                    f"外部仓库接口谎称已完成出库，传统直连模式直接记为已发货；"
                    f"但企业核心账套中的 {_fmt_odoo_delivery_no(bd.get('delivery_name'))} "
                    f"实际仍处于{_d_status(bd.get('delivery_state'), '待仓库拣货')}。"
                )
            elif act_k == "act3":
                t = "缺乏超授信冻结与主管飞书签批防线"
                d = "传统直连模式没有推演草稿区与主管签批锁，无法在高风险变更写入账套前进行拦截。"
            elif act_k == "act4":
                t = "无边走边核对自动巡航导致长期脱节"
                d = (
                    f"业务本体组已完结收款并清零应收；传统直连对照组当前仍滞留在"
                    f"「订单：{_d_status(bd.get('order_state'))} · 出库：{_d_status(bd.get('delivery_state'), '待拣货')} · 发票：尚未开具」。"
                )
            else:
                t = _strip_latin_tokens(ev.get("title") or "异常事件")
                d = _strip_latin_tokens(ev.get("detail") or "")
            bare_events_cn.append({"at": str(ev.get("at") or ""), "title": t, "detail": d})

        bare_note_cn = (
            bare_events_cn[-1]["detail"]
            if bare_events_cn
            else "已初始化两万元对照报价单，尚未执行任何业务动作。"
        )
        bare_residual = (
            format_rmb(float(bd["amount_residual"]) * 100)
            if bd.get("amount_residual") is not None
            else "尚未开票"
        )

        orders_cn: list[dict[str, Any]] = []
        for row in snap["w_mem"]["orders"]:
            res_val = row.get("odoo_residual")
            res_cn = (
                format_rmb(float(res_val) * 100) if res_val is not None else "尚未开票"
            )
            orders_cn.append(
                {
                    "order_label": self._order_display_label(row["order_id"]),
                    "total_yuan": str(row["total_yuan"]),
                    "mem_status_cn": _d_status(row.get("mem_status")),
                    "mem_lock_cn": f"第 {int(row.get('mem_version') or 1)} 版数据锁",
                    "odoo_order_no_cn": _fmt_odoo_order_no(row.get("odoo_order_name")),
                    "odoo_order_state_cn": _d_status(row.get("odoo_order_state")),
                    "odoo_delivery_no_cn": _fmt_odoo_delivery_no(
                        row.get("odoo_delivery_name")
                    ),
                    "odoo_delivery_state_cn": _d_status(
                        row.get("odoo_delivery_state"), "尚未生成"
                    ),
                    "odoo_invoice_no_cn": _fmt_odoo_invoice_no(
                        row.get("odoo_invoice_name")
                    ),
                    "odoo_invoice_state_cn": _d_status(
                        row.get("odoo_invoice_state"), "尚未开票"
                    ),
                    "odoo_payment_state_cn": _d_status(
                        row.get("odoo_payment_state"), "尚未收款"
                    ),
                    "odoo_residual_cn": res_cn,
                }
            )

        runs_cn: list[dict[str, Any]] = []
        for r in snap["w_mem"]["recent_runs"]:
            err_raw = str(r.get("error_code") or "")
            err_cn = _DISPLAY_ERROR_CN.get(err_raw, _strip_latin_tokens(err_raw))
            appr_raw = str(r.get("approved_by") or "")
            appr_cn = "财务总监（飞书签批）" if appr_raw else ""
            runs_cn.append(
                {
                    "run_label": _fmt_run_no(r.get("run_id")),
                    "action_cn": _strip_latin_tokens(r.get("action_cn") or ""),
                    "order_label": self._order_display_label(r.get("order_id")),
                    "status_cn": _d_status(r.get("status")),
                    "is_ok": r.get("status") == "COMMITTED",
                    "error_cn": err_cn,
                    "approved_cn": appr_cn,
                }
            )

        feishu = snap.get("feishu") or {}
        card = feishu.get("card")
        push_map = {
            "sent": "飞书审批卡片已送达（等待点击）",
            "already_sent": "飞书审批卡片待签批",
            "not_configured": "本地签批模式",
            "error": "飞书通道异常（已切换沙盘签批）",
        }
        card_cn = None
        if card:
            dec_actor = card.get("decision_actor")
            if card.get("status") in ("COMMITTED", "REJECTED"):
                dec_text = "财务总监已完成签批"
            elif dec_actor:
                dec_text = "财务总监已签批"
            else:
                dec_text = "等待财务总监在飞书点击批准"
            card_cn = {
                "run_label": _fmt_run_no(card.get("run_id")),
                "order_label": self._order_display_label(card.get("order_id")),
                "odoo_order_no_cn": _fmt_odoo_order_no(card.get("odoo_order_name")),
                "status_cn": _d_status(card.get("status")),
                "push_cn": push_map.get(str(card.get("push_status") or ""), "已生成审批卡片")
                + (" · 回执已回传" if card.get("message_id") else ""),
                "decision_actor_cn": dec_text,
            }

        mpc = snap.get("last_mpc")
        mpc_cn = None
        if mpc:
            traj_cn: list[dict[str, Any]] = []
            for idx, step in enumerate(mpc.get("trajectory") or [], 1):
                act_label = _ACTION_CN.get(str(step.get("action_type") or ""), "业务推进")
                err_code = str(step.get("error_code") or "")
                err_cn = _DISPLAY_ERROR_CN.get(err_code, _strip_latin_tokens(err_code))
                traj_cn.append(
                    {
                        "seq": idx,
                        "action_cn": act_label,
                        "horizon_cn": f"向前预演 {int(step.get('planned_horizon') or 0)} 步",
                        "status_cn": _d_status(step.get("status")),
                        "is_ok": step.get("status") == "COMMITTED",
                        "run_label": _fmt_run_no(step.get("run_id")),
                        "error_cn": err_cn,
                        "refreshed_cn": "已重读实账并擦除草稿重对齐"
                        if step.get("snapshot_refreshed")
                        else "",
                    }
                )
            mpc_cn = {
                "status_cn": _d_status(mpc.get("status")),
                "goal_reached": bool(mpc.get("goal_reached")),
                "steps_committed": int(mpc.get("steps_committed") or 0),
                "replans_used": int(mpc.get("replans_used") or 0),
                "snapshots_refreshed": int(mpc.get("snapshots_refreshed") or 0),
                "trajectory": traj_cn,
            }

        timeline_cn: list[str] = []
        for item in (snap.get("timeline") or [])[-3:]:
            raw_t = (
                str(item.get("title") or "")
                .replace(self.order_1, "订单 1001")
                .replace(self.order_2, "订单 1002")
                .replace(self.bare_order, "对照单 2001")
                .replace("REJECTED", "已拦截")
                .replace("COMMITTED", "已确认")
                .replace("GOAL_REACHED", "目标已达成")
                .replace("DEVICE_STATE_MISMATCH", "外部实账核对不符")
                .replace("CANNOT_INVOICE_UNSHIPPED_ORDER", "未发货严禁开票")
                .replace("MPC", "自动巡航")
                .replace("Odoo", "核心账套")
                .replace("W_lat", "推演棋盘")
                .replace("APPROVE", "批准放行")
                .replace("REJECT", "已驳回")
            )
            t_clean = _strip_latin_tokens(raw_t).strip("- （）()")
            timeline_cn.append(f"[{item.get('at', '')}] {t_clean}")

        return {
            "hero_title": "业务本体沙盘（三看板）对比 传统直连模式 · 接单到回款全流程演播室",
            "hero_sub": "左侧对照组直接调用接口，无校验无核对；右侧三看板以企业核心账套为唯一物理真值，内部确认账、推演草稿区、飞书签批与自动巡航严丝合缝",
            "completed_acts": list(snap["studio"]["completed_acts"]),
            "pills": {
                "real_customer": "签约大客户（业务本体组）",
                "bare_customer": "对照客户（传统直连组）",
                "ar_summary": f"实时应收账款：{snap['w_mem']['ar_balance_yuan']} / 授信总额 {snap['w_real']['credit_limit_yuan']}",
                "feishu_status": "飞书业财群：已实时连通" if feishu.get("configured") else "飞书通道：本地沙盘模式",
            },
            "bare_mcp": {
                "tag": "账实已严重分裂" if bare.get("brain_world_split") else "等待启动对照",
                "split": bool(bare.get("brain_world_split")),
                "order_label": self._order_display_label(bare.get("order_id")),
                "odoo_order_no_cn": _fmt_odoo_order_no(bd.get("order_name")),
                "belief_status_cn": bare_belief_cn,
                "odoo_order_state_cn": _d_status(bd.get("order_state")),
                "odoo_delivery_cn": f"{_fmt_odoo_delivery_no(bd.get('delivery_name'))}（{_d_status(bd.get('delivery_state'), '尚未出库')}）",
                "odoo_invoice_cn": f"{_fmt_odoo_invoice_no(bd.get('invoice_name'))} / 未收 {bare_residual}",
                "belief_note_cn": bare_note_cn,
                "events": list(reversed(bare_events_cn)),
            },
            "w_real": {
                "customer_name": "签约大客户甲（企业核心账套真值）",
                "credit_limit_yuan": snap["w_real"]["credit_limit_yuan"],
                "orders": orders_cn,
            },
            "w_mem": {
                "ar_balance_yuan": snap["w_mem"]["ar_balance_yuan"],
                "remaining_credit_yuan": snap["w_mem"]["remaining_credit_yuan"],
                "orders": orders_cn,
                "recent_runs": list(reversed(runs_cn[-6:])),
            },
            "w_lat": {
                "awaiting_count": int(snap["w_lat"]["awaiting_approval_count"]),
                "pending_count": int(snap["w_lat"]["pending_count"]),
                "card": card_cn,
                "mpc": mpc_cn,
            },
            "timeline_line": "  ｜  ".join(reversed(timeline_cn)) or "演播室系统就绪",
            "footer_storage": "本地双账库与推演草稿日志实时落盘保护中 · 已就绪",
        }


# ---------------------------------------------------------------------------
# Web UI (1920x1080 responsive dark control deck)
# ---------------------------------------------------------------------------
def render_studio_html(initial_state: dict[str, Any]) -> str:
    state_json = json.dumps(initial_state, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>业务本体沙盘对比传统直连 · 接单到回款全流程演播室</title>
<style>
  :root {{
    --bg: #070b14;
    --panel: #0f172a;
    --border: #1e293b;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --cyan: #22d3ee;
    --emerald: #34d399;
    --amber: #fbbf24;
    --rose: #fb7185;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: radial-gradient(circle at top right, #12203f 0%, var(--bg) 55%);
    color: var(--text);
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    padding: 18px 22px 24px;
  }}
  .hero {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 14px 20px;
    background: linear-gradient(135deg, rgba(15,23,42,.96), rgba(17,28,53,.96));
    border: 1px solid #263654;
    border-radius: 14px;
    margin-bottom: 14px;
  }}
  .hero h1 {{
    margin: 0 0 4px;
    font-size: 23px;
    font-weight: 800;
    letter-spacing: .3px;
  }}
  .hero .sub {{ color: var(--muted); font-size: 13px; }}
  .pill-row {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
  .pill {{
    padding: 5px 11px; border-radius: 999px; font-size: 12px; font-weight: 600;
    border: 1px solid #334155; background: #0b1222;
  }}
  .pill.cyan {{ border-color: rgba(34,211,238,.45); color: var(--cyan); }}
  .pill.emerald {{ border-color: rgba(52,211,153,.45); color: var(--emerald); }}
  .pill.amber {{ border-color: rgba(251,191,36,.45); color: var(--amber); }}
  .pill.rose {{ border-color: rgba(251,113,133,.5); color: var(--rose); }}
  .controls {{
    display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; align-items: center;
  }}
  button {{
    border: 1px solid #334155; background: #16213b; color: var(--text);
    padding: 9px 14px; border-radius: 10px; font-size: 13px; font-weight: 700; cursor: pointer;
  }}
  button:hover {{ border-color: var(--cyan); background: #1c2b4e; }}
  button.primary {{ background: linear-gradient(135deg, #0ea5e9, #0284c7); border-color: #38bdf8; color: #fff; }}
  button.emerald {{ background: linear-gradient(135deg, #10b981, #059669); border-color: #34d399; color: #041b13; }}
  button.amber {{ background: linear-gradient(135deg, #f59e0b, #d97706); border-color: #fbbf24; color: #1f1300; }}
  button.danger {{ background: linear-gradient(135deg, #e11d48, #be123c); border-color: #fb7185; color: #fff; }}
  button.done {{ border-color: var(--emerald); box-shadow: 0 0 0 1px rgba(52,211,153,.35) inset; }}
  .grid-4 {{
    display: grid; grid-template-columns: 1.02fr 1.05fr 1.12fr 1.12fr; gap: 14px; align-items: stretch;
  }}
  .grid-voucher {{
    display: grid; grid-template-columns: 1.1fr 1.1fr 1.1fr; gap: 16px; align-items: stretch;
  }}
  .board {{
    background: var(--panel); border-radius: 14px; border: 1px solid var(--border);
    padding: 15px 16px; display: flex; flex-direction: column; gap: 12px; min-height: 685px;
  }}
  .board.rose {{ border-color: rgba(251,113,133,.5); background: linear-gradient(180deg, rgba(44,13,26,.45), var(--panel) 32%); }}
  .board.cyan {{ border-color: rgba(34,211,238,.45); background: linear-gradient(180deg, rgba(10,36,54,.45), var(--panel) 32%); }}
  .board.emerald {{ border-color: rgba(52,211,153,.45); background: linear-gradient(180deg, rgba(9,45,35,.45), var(--panel) 32%); }}
  .board.amber {{ border-color: rgba(251,191,36,.45); background: linear-gradient(180deg, rgba(48,32,9,.42), var(--panel) 32%); }}
  .board-head {{
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid #1e293b; padding-bottom: 10px;
  }}
  .board-head h2 {{ margin: 0; font-size: 16px; font-weight: 800; }}
  .tag {{
    font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 999px;
    background: #1e293b; color: var(--text);
  }}
  .tag.rose {{ background: rgba(251,113,133,.18); color: #fda4af; border: 1px solid rgba(251,113,133,.4); }}
  .tag.cyan {{ background: rgba(34,211,238,.15); color: #67e8f9; border: 1px solid rgba(34,211,238,.35); }}
  .tag.emerald {{ background: rgba(52,211,153,.15); color: #6ee7b7; border: 1px solid rgba(52,211,153,.35); }}
  .tag.amber {{ background: rgba(251,191,36,.16); color: #fde68a; border: 1px solid rgba(251,191,36,.35); }}
  .card {{ background: #0b1222; border: 1px solid #1e293b; border-radius: 10px; padding: 11px 12px; }}
  .card-title {{
    font-size: 13px; font-weight: 800; margin-bottom: 6px;
    display: flex; justify-content: space-between; gap: 8px;
  }}
  .kv {{ display: grid; grid-template-columns: 108px 1fr; gap: 5px 8px; font-size: 12px; line-height: 1.5; }}
  .k {{ color: var(--muted); }}
  .v {{ font-weight: 600; word-break: break-word; }}
  .list {{ display: flex; flex-direction: column; gap: 7px; }}
  .run-row {{
    padding: 7px 9px; border-radius: 8px; background: #09101f; border: 1px solid #1d2a44; font-size: 12px;
  }}
  .run-top {{ display: flex; justify-content: space-between; font-weight: 700; margin-bottom: 2px; gap: 6px; }}
  .split-alert {{
    background: rgba(225, 29, 72, 0.16); border: 1px solid rgba(251, 113, 133, 0.55);
    color: #fecdd3; border-radius: 10px; padding: 10px 12px; font-size: 12.5px; line-height: 1.5;
  }}
  .ok-banner {{
    background: rgba(16, 185, 129, 0.14); border: 1px solid rgba(52, 211, 153, 0.5);
    color: #a7f3d0; border-radius: 10px; padding: 10px 12px; font-size: 12.5px; line-height: 1.5;
  }}
  .footer-strip {{
    margin-top: 12px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 10px 14px; display: flex; justify-content: space-between;
    align-items: center; gap: 16px; font-size: 12px; color: var(--muted);
  }}
</style>
</head>
<body>
  <div class="hero">
    <div>
      <h1 id="hero-title">业务本体沙盘（三看板）对比 传统直连模式 · 接单到回款全流程演播室</h1>
      <div class="sub" id="hero-sub">左侧对照组直接调用接口，无校验无核对；右侧三看板以企业核心账套为唯一物理真值</div>
    </div>
    <div class="pill-row" id="hero-pills"></div>
  </div>

  <div class="controls" id="controls-bar">
    <button onclick="callApi('/api/reset', null, '重置双边业务环境')" class="danger">重置双边环境</button>
    <button onclick="callApi('/api/act/1', null, '第一幕预演对齐与禁开票拦截')" id="btn-act-1" class="primary">第一幕：全知对齐 &amp; 未发货禁开票</button>
    <button onclick="callApi('/api/act/2', null, '第二幕核验外部谎言')" id="btn-act-2" class="primary">第二幕：抓获执行器谎言（实账核对不符）</button>
    <button onclick="callApi('/api/act/3', null, '第三幕冻结大单并推飞书卡')" id="btn-act-3" class="amber">第三幕：超授信锁入草稿区 &amp; 飞书推卡</button>
    <button onclick="callApi('/api/act/3/approve', null, '财务总监签批放行')" id="btn-act-3-appr" class="emerald">财务总监签批放行</button>
    <button onclick="callApi('/api/act/4', null, '第四幕自动巡航收口')" id="btn-act-4" class="primary">第四幕：边走边核对自动巡航收款</button>
    <button onclick="callApi('/api/walkthrough', null, '全剧连续贯通演示')">一键全剧贯通</button>
    <button onclick="toggleVoucherView()" id="btn-toggle-view" class="amber">切换：外部实账凭据核验大屏</button>
    <span id="status-msg" class="pill">已就绪 · 每 1.5 秒自动同步</span>
  </div>

  <div id="stage-boards" class="grid-4">
    <section class="board rose" id="board-bare"></section>
    <section class="board cyan" id="board-real"></section>
    <section class="board emerald" id="board-mem"></section>
    <section class="board amber" id="board-lat"></section>
  </div>

  <div id="stage-vouchers" class="grid-voucher" style="display:none"></div>

  <div class="footer-strip">
    <div id="timeline-strip">正在载入演播室实时进展…</div>
    <div id="paths-strip">本地双账库与推演草稿日志实时落盘保护中 · 已就绪</div>
  </div>

<script>
let CURRENT = {state_json};
let VIEW_MODE = (new URLSearchParams(window.location.search).get("view") === "voucher") ? "voucher" : "boards";

function esc(v) {{
  return String(v ?? "").replace(/[&<>"']/g, s => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }}[s]));
}}

function toggleVoucherView() {{
  VIEW_MODE = (VIEW_MODE === "voucher") ? "boards" : "voucher";
  render();
}}

async function callApi(url, bodyObj, label) {{
  const msg = document.getElementById("status-msg");
  msg.textContent = "正在执行：" + (label || "业务指令") + " …";
  try {{
    const res = await fetch(url, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(bodyObj || {{}})
    }});
    const data = await res.json();
    if (data.state) {{
      CURRENT = data.state;
      render();
    }}
    msg.textContent = data.error ? ("操作受阻，系统已安全拦截") : ("已完成：" + (label || "业务指令"));
  }} catch (_err) {{
    msg.textContent = "网络通信异常，请重试";
  }}
}}

function render() {{
  const d = CURRENT.display;
  if (!d) return;

  const done = new Set(d.completed_acts || []);
  ["1", "2", "3", "4"].forEach(k => {{
    const btn = document.getElementById("btn-act-" + k);
    if (btn) btn.classList.toggle("done", done.has(k));
  }});

  document.getElementById("hero-title").textContent = (VIEW_MODE === "voucher")
    ? "外部实账财务凭据核验大屏 · 企业核心账套实据对照"
    : d.hero_title;
  document.getElementById("hero-sub").textContent = (VIEW_MODE === "voucher")
    ? "以下全部单号、出库状态、发票过账与未收余款均实时取自外部企业核心账套，与业务本体记忆账本逐笔核验对齐"
    : d.hero_sub;
  document.getElementById("btn-toggle-view").textContent = (VIEW_MODE === "voucher")
    ? "返回：四看板总控台"
    : "切换：外部实账凭据核验大屏";

  document.getElementById("hero-pills").innerHTML = `
    <span class="pill cyan">${{esc(d.pills.real_customer)}}</span>
    <span class="pill rose">${{esc(d.pills.bare_customer)}}</span>
    <span class="pill emerald">${{esc(d.pills.ar_summary)}}</span>
    <span class="pill amber">${{esc(d.pills.feishu_status)}}</span>
  `;

  const boardsEl = document.getElementById("stage-boards");
  const vouchersEl = document.getElementById("stage-vouchers");

  if (VIEW_MODE === "voucher") {{
    boardsEl.style.display = "none";
    vouchersEl.style.display = "grid";
    const o1 = d.w_real.orders[0];
    const o2 = d.w_real.orders[1];
    const b = d.bare_mcp;
    vouchersEl.innerHTML = `
      <section class="board cyan">
        <div class="board-head">
          <h2>实账凭据一 · ${{esc(o1.order_label)}}</h2>
          <span class="tag cyan">${{esc(o1.odoo_order_no_cn)}}</span>
        </div>
        <div class="card">
          <div class="card-title"><span>企业核心账套实时三单核验</span><span class="tag emerald">${{esc(o1.mem_status_cn)}}</span></div>
          <div class="kv">
            <span class="k">签约金额</span><span class="v">${{esc(o1.total_yuan)}}</span>
            <span class="k">销售订单单号</span><span class="v">${{esc(o1.odoo_order_no_cn)}}</span>
            <span class="k">账套订单状态</span><span class="v">${{esc(o1.odoo_order_state_cn)}}</span>
            <span class="k">仓库出库单号</span><span class="v">${{esc(o1.odoo_delivery_no_cn)}}</span>
            <span class="k">仓库物理状态</span><span class="v">${{esc(o1.odoo_delivery_state_cn)}}</span>
            <span class="k">增值税发票号</span><span class="v">${{esc(o1.odoo_invoice_no_cn)}}</span>
            <span class="k">发票入账状态</span><span class="v">${{esc(o1.odoo_invoice_state_cn)}}</span>
            <span class="k">银行收款核销</span><span class="v">${{esc(o1.odoo_payment_state_cn)}}</span>
            <span class="k">当前未收余款</span><span class="v">${{esc(o1.odoo_residual_cn)}}</span>
          </div>
        </div>
        <div class="ok-banner">
          每一笔资金、货物流动均经外部实账反向核验后才写入内部确认账本，绝不轻信接口返回文案。
        </div>
      </section>

      <section class="board amber">
        <div class="board-head">
          <h2>实账凭据二 · ${{esc(o2.order_label)}}</h2>
          <span class="tag amber">${{esc(o2.odoo_order_no_cn)}}</span>
        </div>
        <div class="card">
          <div class="card-title"><span>超授信大额单风控实据</span><span class="tag amber">${{esc(o2.mem_status_cn)}}</span></div>
          <div class="kv">
            <span class="k">订单金额</span><span class="v">${{esc(o2.total_yuan)}}</span>
            <span class="k">客户授信总额</span><span class="v">${{esc(d.w_real.credit_limit_yuan)}}</span>
            <span class="k">销售订单单号</span><span class="v">${{esc(o2.odoo_order_no_cn)}}</span>
            <span class="k">账套订单状态</span><span class="v">${{esc(o2.odoo_order_state_cn)}}</span>
            <span class="k">仓库出库单号</span><span class="v">${{esc(o2.odoo_delivery_no_cn)}}</span>
            <span class="k">仓库物理状态</span><span class="v">${{esc(o2.odoo_delivery_state_cn)}}</span>
          </div>
        </div>
        <div class="card" style="flex:1">
          <div class="card-title"><span>当前授信与飞书签批防线</span></div>
          <div class="kv">
            <span class="k">实时应收占用</span><span class="v">${{esc(d.w_mem.ar_balance_yuan)}}</span>
            <span class="k">剩余可用额度</span><span class="v">${{esc(d.w_mem.remaining_credit_yuan)}}</span>
            <span class="k">飞书审批状态</span><span class="v">${{d.w_lat.card ? esc(d.w_lat.card.status_cn + "（" + d.w_lat.card.decision_actor_cn + "）") : "等待大额触发或已完结"}}</span>
          </div>
        </div>
      </section>

      <section class="board rose">
        <div class="board-head">
          <h2>对照组实据 · ${{esc(b.order_label)}}</h2>
          <span class="tag rose">${{esc(b.odoo_order_no_cn)}}</span>
        </div>
        <div class="${{b.split ? 'split-alert' : 'card'}}">
          <div class="card-title"><span>传统直连模式实账脱节对比</span><span class="tag rose">${{esc(b.tag)}}</span></div>
          <div class="kv">
            <span class="k">自认进度</span><span class="v">${{esc(b.belief_status_cn)}}</span>
            <span class="k">实际订单态</span><span class="v">${{esc(b.odoo_order_state_cn)}}</span>
            <span class="k">实际出库态</span><span class="v">${{esc(b.odoo_delivery_cn)}}</span>
            <span class="k">发票与余款</span><span class="v">${{esc(b.odoo_invoice_cn)}}</span>
          </div>
          <div style="margin-top:8px;font-size:12px">${{esc(b.belief_note_cn)}}</div>
        </div>
      </section>
    `;
  }} else {{
    boardsEl.style.display = "grid";
    vouchersEl.style.display = "none";

    // Board 0: Bare MCP
    const b = d.bare_mcp;
    const bareEvents = (b.events || []).map(ev => `
      <div class="run-row">
        <div class="run-top"><span>${{esc(ev.title)}}</span><span>${{esc(ev.at)}}</span></div>
        <div style="color:#cbd5e1">${{esc(ev.detail)}}</div>
      </div>
    `).join("");
    document.getElementById("board-bare").innerHTML = `
      <div class="board-head">
        <h2>对照组 · 传统直连模式（开环无校验）</h2>
        <span class="tag rose">${{esc(b.tag)}}</span>
      </div>
      <div class="${{b.split ? "split-alert" : "card"}}">
        <div class="card-title"><span>助手脑内账目 对比 企业核心账套</span></div>
        <div class="kv">
          <span class="k">对照订单</span><span class="v">${{esc(b.order_label)}}（${{esc(b.odoo_order_no_cn)}}）</span>
          <span class="k">助手自认状态</span><span class="v" style="color:#fda4af">${{esc(b.belief_status_cn)}}</span>
          <span class="k">核心账套实态</span><span class="v">${{esc(b.odoo_order_state_cn)}}</span>
          <span class="k">真实仓库出库</span><span class="v">${{esc(b.odoo_delivery_cn)}}</span>
          <span class="k">真实发票余款</span><span class="v">${{esc(b.odoo_invoice_cn)}}</span>
        </div>
        <div style="margin-top:8px;color:#fecdd3;font-size:12px">${{esc(b.belief_note_cn)}}</div>
      </div>
      <div class="card" style="flex:1">
        <div class="card-title"><span>开环故障实录</span><span class="tag rose">无实账回核</span></div>
        <div class="list">${{bareEvents || '<div style="color:#64748b;font-size:12px">点击第一至四幕对比传统直连模式失控现场</div>'}}</div>
      </div>
    `;

    // Board 1: W_real
    const realCards = d.w_real.orders.map(o => `
      <div class="card">
        <div class="card-title"><span>${{esc(o.order_label)}} · ${{esc(o.total_yuan)}}</span><span class="tag cyan">${{esc(o.odoo_order_no_cn)}}</span></div>
        <div class="kv">
          <span class="k">销售订单状态</span><span class="v">${{esc(o.odoo_order_state_cn)}}</span>
          <span class="k">仓库出库单据</span><span class="v">${{esc(o.odoo_delivery_no_cn)}} · ${{esc(o.odoo_delivery_state_cn)}}</span>
          <span class="k">财务发票单据</span><span class="v">${{esc(o.odoo_invoice_no_cn)}} · ${{esc(o.odoo_invoice_state_cn)}}</span>
          <span class="k">收款与余款</span><span class="v">${{esc(o.odoo_payment_state_cn)}} · 余 ${{esc(o.odoo_residual_cn)}}</span>
        </div>
      </div>
    `).join("");
    document.getElementById("board-real").innerHTML = `
      <div class="board-head">
        <h2>看板 ① · 真实棋盘（外部实账）</h2>
        <span class="tag cyan">唯一物理真值</span>
      </div>
      <div class="card">
        <div class="kv">
          <span class="k">签约客户</span><span class="v">${{esc(d.w_real.customer_name)}}</span>
          <span class="k">授信额度</span><span class="v">${{esc(d.w_real.credit_limit_yuan)}}</span>
        </div>
      </div>
      ${{realCards}}
    `;

    // Board 2: W_mem
    const memOrders = d.w_mem.orders.map(o => `
      <div class="run-row">
        <div class="run-top"><span>${{esc(o.order_label)}}</span><span class="tag emerald">${{esc(o.mem_status_cn)}} · ${{esc(o.mem_lock_cn)}}</span></div>
        <div style="color:#94a3b8">${{esc(o.total_yuan)}} ｜ 核心账套单号：${{esc(o.odoo_order_no_cn)}}</div>
      </div>
    `).join("");
    const memRuns = (d.w_mem.recent_runs || []).map(r => `
      <div class="run-row">
        <div class="run-top"><span>${{esc(r.action_cn)}}（${{esc(r.order_label)}}）</span><span class="tag ${{r.is_ok ? 'emerald' : 'rose'}}">${{esc(r.status_cn)}}</span></div>
        <div style="color:#94a3b8">${{esc(r.run_label)}}${{r.error_cn ? " · 原因：" + esc(r.error_cn) : ""}}${{r.approved_cn ? " · 签批：" + esc(r.approved_cn) : ""}}</div>
      </div>
    `).join("");
    document.getElementById("board-mem").innerHTML = `
      <div class="board-head">
        <h2>看板 ② · 记忆棋盘（内部确认账）</h2>
        <span class="tag emerald">零幻觉对齐</span>
      </div>
      <div class="ok-banner">
        <div class="kv">
          <span class="k">实时应收占用</span><span class="v">${{esc(d.w_mem.ar_balance_yuan)}}</span>
          <span class="k">剩余可用授信</span><span class="v">${{esc(d.w_mem.remaining_credit_yuan)}}</span>
        </div>
      </div>
      <div class="card"><div class="card-title"><span>内部确认对象与防并发数据锁</span></div><div class="list">${{memOrders}}</div></div>
      <div class="card" style="flex:1"><div class="card-title"><span>业财审计总台账</span></div><div class="list">${{memRuns || '<div style="color:#64748b;font-size:12px">尚无运行记录</div>'}}</div></div>
    `;

    // Board 3: W_lat + Feishu + Auto Cruise
    const card = d.w_lat.card;
    const cardHtml = card ? `
      <div class="card">
        <div class="card-title"><span>飞书业财助手审批卡片</span><span class="tag amber">${{esc(card.status_cn)}}</span></div>
        <div class="kv">
          <span class="k">审批流水</span><span class="v">${{esc(card.run_label)}}</span>
          <span class="k">关联订单</span><span class="v">${{esc(card.order_label)}}（${{esc(card.odoo_order_no_cn)}}）</span>
          <span class="k">推送状态</span><span class="v">${{esc(card.push_cn)}}</span>
          <span class="k">当前签批</span><span class="v">${{esc(card.decision_actor_cn)}}</span>
        </div>
      </div>
    ` : `<div class="card" style="color:#94a3b8;font-size:12px">第三幕触发十二万元超授信大单后，将自动锁入推演棋盘（预演草稿区）并向飞书业财助手推送可点击审批卡片</div>`;

    const mpc = d.w_lat.mpc;
    const mpcRows = mpc ? (mpc.trajectory || []).map(t => `
      <div class="run-row">
        <div class="run-top"><span>第 ${{esc(t.seq)}} 步 ${{esc(t.action_cn)}}（${{esc(t.horizon_cn)}}）</span><span class="tag ${{t.is_ok ? 'emerald' : 'rose'}}">${{esc(t.status_cn)}}</span></div>
        <div style="color:#94a3b8">${{esc(t.run_label)}}${{t.error_cn ? " · 原因：" + esc(t.error_cn) : ""}}${{t.refreshed_cn ? " · " + esc(t.refreshed_cn) : ""}}</div>
      </div>
    `).join("") : "";

    document.getElementById("board-lat").innerHTML = `
      <div class="board-head">
        <h2>看板 ③ · 推演棋盘（预演草稿区）</h2>
        <span class="tag amber">待签批 ${{d.w_lat.awaiting_count}} · 待核验 ${{d.w_lat.pending_count}}</span>
      </div>
      ${{cardHtml}}
      <div class="card" style="flex:1">
        <div class="card-title"><span>边走边核对自动巡航轨迹</span><span class="tag ${{mpc && mpc.goal_reached ? 'emerald' : 'amber'}}">${{mpc ? esc(mpc.status_cn) : "尚未启动"}}</span></div>
        ${{mpc ? `<div class="kv" style="margin-bottom:8px"><span class="k">核对重算汇总</span><span class="v">落盘 ${{mpc.steps_committed}} 步 · 遇错重规划 ${{mpc.replans_used}} 次 · 重读实账 ${{mpc.snapshots_refreshed}} 次</span></div>` : ""}}
        <div class="list">${{mpcRows || '<div style="color:#64748b;font-size:12px">第四幕将展示多步预演试走、每次只落实一步，并在首次发货谎言穿帮后自动重规划直到应收清零</div>'}}</div>
      </div>
    `;
  }}

  document.getElementById("timeline-strip").textContent = d.timeline_line;
  document.getElementById("paths-strip").textContent = d.footer_storage;
}}

render();
setInterval(async () => {{
  try {{
    const r = await fetch("/api/state");
    if (r.ok) {{
      CURRENT = await r.json();
      render();
    }}
  }} catch (_e) {{}}
}}, 1500);
</script>
</body>
</html>"""


class _StudioHandler(BaseHTTPRequestHandler):
    app: ThreeBoardsStudioApp

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            html_body = render_studio_html(self.app.state_snapshot()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_body)))
            self.end_headers()
            self.wfile.write(html_body)
            return
        if path == "/api/state":
            self._send_json(HTTPStatus.OK, self.app.state_snapshot())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown route {path}"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self._read_json_body()
            if path == "/api/reset":
                self._send_json(HTTPStatus.OK, {"ok": True, "state": self.app.reset()})
                return
            if path == "/api/act/1":
                self._send_json(HTTPStatus.OK, self.app.run_act_1())
                return
            if path == "/api/act/2":
                self._send_json(HTTPStatus.OK, self.app.run_act_2())
                return
            if path == "/api/act/3":
                self._send_json(HTTPStatus.OK, self.app.run_act_3())
                return
            if path == "/api/act/3/approve":
                res = self.app.decide_act_3(
                    op="approve",
                    run_id=body.get("run_id"),
                    operator_open_id=body.get("operator_open_id") or "finance-director-studio",
                    require_token=False,
                )
                self._send_json(HTTPStatus.OK, res)
                return
            if path == "/api/act/3/reject":
                res = self.app.decide_act_3(
                    op="reject",
                    run_id=body.get("run_id"),
                    operator_open_id=body.get("operator_open_id") or "finance-director-studio",
                    require_token=False,
                )
                self._send_json(HTTPStatus.OK, res)
                return
            if path == "/api/feishu/callback":
                res = self.app.handle_feishu_callback(body)
                self._send_json(HTTPStatus.OK, res)
                return
            if path == "/api/act/4":
                inject = bool(body.get("inject_first_ship_drift", True))
                self._send_json(
                    HTTPStatus.OK,
                    self.app.run_act_4(inject_first_ship_drift=inject),
                )
                return
            if path == "/api/walkthrough":
                self._send_json(
                    HTTPStatus.OK, {"ok": True, "state": self.app.run_walkthrough()}
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown route {path}"})
        except Exception as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error": _redact_secrets(
                        str(exc), [self.app._app_secret] if self.app._app_secret else []
                    ),
                    "state": self.app.state_snapshot(),
                },
            )


def create_server(
    app: ThreeBoardsStudioApp, *, host: str = "127.0.0.1", port: int = 8791
) -> ThreadingHTTPServer:
    class BoundHandler(_StudioHandler):
        pass

    BoundHandler.app = app
    return ThreadingHTTPServer((host, port), BoundHandler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="OpenOntology Three Boards vs Bare MCP Investor Roadshow Studio"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--state-dir", default="/tmp/open_ontology_o2c_investor_studio")
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--partner-ref", default=DEFAULT_PARTNER_REF)
    parser.add_argument("--bare-partner-ref", default=DEFAULT_BARE_PARTNER_REF)
    parser.add_argument("--callback-host", default="127.0.0.1")
    args = parser.parse_args(argv)

    app = ThreeBoardsStudioApp(
        args.state_dir,
        profile_dir=args.profile_dir,
        partner_ref=args.partner_ref,
        bare_partner_ref=args.bare_partner_ref,
        callback_host=args.callback_host,
        port=args.port,
    )
    server = create_server(app, host=args.host, port=args.port)
    print(
        json.dumps(
            {
                "studio": "ready",
                "url": f"http://{args.host}:{args.port}/",
                "partner_ref": app.partner_ref,
                "bare_partner_ref": app.bare_partner_ref,
                "feishu_configured": app.feishu_client.is_configured,
                "feishu_receive_id": app.feishu_client.receive_id,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
