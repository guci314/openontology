#!/usr/bin/env python3
"""Live End-to-End Domestic Role-Agent + Real MCP Server Governance Demo.

Starts a real ``OntologyMCPServer`` over Streamable HTTP hosting:
- ``std_core_ontology + std_crm_ontology + std_o2c_ontology + CustomerOverlaySpec``
  with an overlay invariant blocking ``confirm_o2c_order`` whenever the customer
  party has an ``OPEN`` ``HIGH``/``URGENT`` ``CrmTicket`` (``URGENT_TICKET_BLOCKS_ORDER``)
  and restricting ``resolve_crm_ticket`` away from ``sales_rep``.
- A server-pinned ``sales_rep`` Actor (``rep-lin``).
- 2 draft orders:
  1. ``ORD-SAFE-101`` (50,000 CNY, healthy customer, 0 urgent tickets -> compliant)
  2. ``ORD-RISK-102`` (180,000 CNY, blocked by urgent ``CrmTicket`` ``TK-URG-901``)

Connects the local real ``hermes`` CLI inside a temporary isolated ``HERMES_HOME``
configured with a domestic model channel (OpenRouter ``deepseek/deepseek-chat-v3-0324``
or DeepSeek direct, strictly avoiding exhausted ``opencode-go`` and Gemini, zero mocks)
via native ``mcp_servers`` Streamable HTTP transport.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from open_ontology import Actor, OntologyRuntime  # noqa: E402
from open_ontology.induction import (  # noqa: E402
    CustomerOverlaySpec,
    InvariantRuleSpec,
    apply_overlay,
    compile_blueprint,
    compose_blueprints,
)
from open_ontology.mcp_server import OntologyMCPServer, start_mcp_http_server  # noqa: E402

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY  # noqa: E402
from examples.std_core_ontology import build_std_core_blueprint  # noqa: E402
from examples.std_crm_ontology import build_std_crm_blueprint  # noqa: E402
from examples.std_o2c_ontology import build_std_o2c_blueprint  # noqa: E402


def _ensure_local_no_proxy() -> None:
    for env_key in ("NO_PROXY", "no_proxy"):
        items = [x.strip() for x in os.environ.get(env_key, "").split(",") if x.strip()]
        for host in ("127.0.0.1", "localhost", "::1"):
            if host not in items:
                items.append(host)
        os.environ[env_key] = ",".join(items)


def _read_key_readonly(key_name: str) -> str:
    val = os.environ.get(key_name, "").strip()
    if val:
        return val
    user_env = Path.home() / ".hermes" / ".env"
    if not user_env.is_file():
        return ""
    for line in user_env.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        if k.strip() == key_name:
            return v.strip().strip('"').strip("'")
    return ""


def _select_live_domestic_channel() -> tuple[dict[str, Any], dict[str, str]]:
    """Preflight domestic Chinese LLM endpoints cleanly without touching opencode-go or Gemini."""
    candidates: list[dict[str, Any]] = []
    or_key = _read_key_readonly("OPENROUTER_API_KEY")
    if or_key:
        candidates.append(
            {
                "name": "openrouter-deepseek-v3",
                "provider": "openrouter",
                "model": "deepseek/deepseek-chat-v3-0324",
                "probe_url": "https://openrouter.ai/api/v1/chat/completions",
                "env_vars": {"OPENROUTER_API_KEY": or_key},
                "config_model": {
                    "default": "deepseek/deepseek-chat-v3-0324",
                    "provider": "openrouter",
                },
                "auth_key": or_key,
            }
        )
    ds_key = _read_key_readonly("DEEPSEEK_API_KEY")
    if ds_key:
        candidates.append(
            {
                "name": "deepseek-direct",
                "provider": "custom",
                "model": "deepseek-chat",
                "probe_url": "https://api.deepseek.com/v1/chat/completions",
                "env_vars": {"OPENAI_API_KEY": ds_key, "DEEPSEEK_API_KEY": ds_key},
                "config_model": {
                    "default": "deepseek-chat",
                    "provider": "custom",
                    "base_url": "https://api.deepseek.com/v1",
                },
                "auth_key": ds_key,
            }
        )

    errors: list[str] = []
    for cand in candidates:
        req = urllib.request.Request(
            cand["probe_url"],
            data=json.dumps(
                {
                    "model": cand["model"],
                    "messages": [{"role": "user", "content": "OK"}],
                    "max_tokens": 4,
                }
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cand['auth_key']}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    return cand, dict(cand["env_vars"])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            errors.append(f"{cand['name']}: {type(exc).__name__} ({exc})")

    raise RuntimeError(
        "No live non-Gemini domestic LLM channel passed preflight: "
        + ("; ".join(errors) if errors else "no OPENROUTER_API_KEY/DEEPSEEK_API_KEY found")
    )


def build_governed_sales_runtime() -> tuple[OntologyRuntime, dict[str, Actor]]:
    base = compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
        ],
        model_id="live_domestic_agent_o2c_crm",
        version="1.0.0",
    )
    overlay = CustomerOverlaySpec(
        overlay_id="steel_sales_ticket_blocker_overlay",
        base_model_id=base.model_id,
        target_version="1.1.0",
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="no_open_urgent_ticket_before_order_confirm",
                    code="URGENT_TICKET_BLOCKS_ORDER",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "target.account.party"}, "right": {"ref": "obj.customer.party"}},
                                {"op": "in", "left": {"ref": "target.priority"}, "right": {"lit": ["HIGH", "URGENT"]}},
                                {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                            ],
                        },
                    },
                    description="Customer party has unresolved HIGH/URGENT CrmTicket; confirm_o2c_order is blocked until support resolves or manager approves via WorkCard.",
                )
            ]
        },
        restrict_allowed_roles={
            "resolve_crm_ticket": ["support_agent", "ops", "steward", "admin"],
        },
    )
    governed_bp = apply_overlay(base, overlay)
    spec = compile_blueprint(governed_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    rt = OntologyRuntime(spec, db_path=":memory:")

    admin = Actor(actor_id="sys-admin", role="admin", clearance_tags={"*"})
    sales_rep = Actor(actor_id="rep-lin", role="sales_rep", clearance_tags={"*"})

    ent_cls = spec.get_object_type("AccountingEntity")
    party_cls = spec.get_object_type("BusinessParty")
    acc_cls = spec.get_object_type("CrmAccount")
    tk_cls = spec.get_object_type("CrmTicket")
    prof_cls = spec.get_object_type("O2cCustomerProfile")
    ord_cls = spec.get_object_type("O2cOrder")

    rt.put_object(ent_cls(entity_id="ENT-CN-EAST", name="华东钢贸法人主体", base_currency="CNY"), actor=admin)

    # Order 1 customer: clean compliance state
    rt.put_object(
        party_cls(party_id="PTY-SAFE", legal_name="沪杭路桥工程有限公司", tax_id="91310000111122223A"),
        actor=admin,
    )
    rt.put_object(acc_cls(account_id="ACC-SAFE", name="沪杭路桥", party="PTY-SAFE", industry="Infrastructure"), actor=admin)
    rt.put_object(
        prof_cls(
            profile_id="PROF-SAFE",
            party="PTY-SAFE",
            accounting_entity="ENT-CN-EAST",
            credit_limit=800_000.0,
            payment_terms="NET30",
        ),
        actor=admin,
    )
    rt.put_object(
        ord_cls(
            order_id="ORD-SAFE-101",
            order_number="SO-2026-SAFE-101",
            customer="PROF-SAFE",
            accounting_entity="ENT-CN-EAST",
            amount_total=50_000.0,
            status="draft",
        ),
        actor=admin,
    )

    # Order 2 customer: open URGENT ticket blocking confirm_o2c_order
    rt.put_object(
        party_cls(party_id="PTY-RISK", legal_name="江海重工机械有限公司", tax_id="91320000444455556B"),
        actor=admin,
    )
    rt.put_object(acc_cls(account_id="ACC-RISK", name="江海重工", party="PTY-RISK", industry="HeavyIndustry"), actor=admin)
    rt.put_object(
        prof_cls(
            profile_id="PROF-RISK",
            party="PTY-RISK",
            accounting_entity="ENT-CN-EAST",
            credit_limit=1_000_000.0,
            payment_terms="NET30",
        ),
        actor=admin,
    )
    rt.put_object(
        tk_cls(
            ticket_id="TK-URG-901",
            subject="特种焊接钢材探伤报告异常告警",
            account="ACC-RISK",
            priority="URGENT",
            status="OPEN",
        ),
        actor=admin,
    )
    rt.put_object(
        ord_cls(
            order_id="ORD-RISK-102",
            order_number="SO-2026-RISK-102",
            customer="PROF-RISK",
            accounting_entity="ENT-CN-EAST",
            amount_total=180_000.0,
            status="draft",
        ),
        actor=admin,
    )
    return rt, {"admin": admin, "sales_rep": sales_rep}


def run_live_demo() -> dict[str, Any]:
    _ensure_local_no_proxy()
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        raise FileNotFoundError("hermes CLI not found in PATH")

    ver_proc = subprocess.run([hermes_bin, "--version"], capture_output=True, text=True, timeout=15)
    hermes_version = (ver_proc.stdout or ver_proc.stderr).strip().splitlines()[0] if (ver_proc.stdout or ver_proc.stderr) else "unknown"

    channel, secret_env = _select_live_domestic_channel()
    rt, actors = build_governed_sales_runtime()
    mcp_srv = OntologyMCPServer(rt, actors["sales_rep"], include_ui_tools=False)

    isolated_home = Path(tempfile.mkdtemp(prefix="oo_hermes_mcp_"))
    t0 = time.monotonic()
    try:
        with start_mcp_http_server(mcp_srv, host="127.0.0.1", port=0) as hdl:
            model_cfg = channel["config_model"]
            cfg_lines = [
                "model:",
                f"  default: {model_cfg['default']}",
                f"  provider: {model_cfg['provider']}",
            ]
            if "base_url" in model_cfg:
                cfg_lines.append(f"  base_url: {model_cfg['base_url']}")
            cfg_lines.extend(
                [
                    "memory:",
                    "  memory_enabled: false",
                    "  user_profile_enabled: false",
                    "mcp_servers:",
                    "  open_ontology:",
                    f"    url: {hdl.mcp_url}",
                    "    enabled: true",
                    "    trust: full",
                    "    connect_timeout: 15",
                ]
            )
            (isolated_home / "config.yaml").write_text("\n".join(cfg_lines) + "\n", encoding="utf-8")

            env_lines = [f"{k}={v}" for k, v in secret_env.items()]
            env_lines.append("NO_PROXY=127.0.0.1,localhost,::1")
            env_lines.append("no_proxy=127.0.0.1,localhost,::1")
            env_file = isolated_home / ".env"
            env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
            os.chmod(env_file, 0o600)

            prompt = (
                "你是华东钢贸的销售代表（sales_rep，actor_id=rep-lin）。"
                "现在只能通过 `open_ontology` MCP 工具办理待审核草稿销售订单（严禁尝试越权关闭客服工单）：\n"
                "1. 先调用 `describe_tbox` 了解当前权限内的对象模型与动作约束，再调用 `query_objects` 查询全部 `O2cOrder`；\n"
                "2. 对每笔草稿订单先调用 `simulate_action`（action=`confirm_o2c_order`, params=`{\"order_id\": ...}`）做零副作用沙盘推演；\n"
                "3. 对推演通过的合规订单，调用 `submit_action`（传入唯一 `run_id`，action=`confirm_o2c_order`）正式提交确认；\n"
                "4. 若某笔订单在 `simulate_action` 或 `submit_action` 时被硬不变量拦截（如未关闭的紧急客诉工单 `URGENT_TICKET_BLOCKS_ORDER`），"
                "绝对不要尝试绕过或代为关闭工单；而是调用 `submit_action`（action=`open_work_card`），参数为：\n"
                "   card_id=`WC-ORD-RISK-102`, card_kind=`HUMAN_TASK`, target_object_type=`O2cOrder`, "
                "target_object_id=<被拦截订单order_id>, requested_action=`confirm_o2c_order`, "
                "requested_params_json=`{\"order_id\": \"...\"}`, assignee_role=`sales_manager`。\n"
                "完成后用简短中文汇报每笔订单的处理结果。"
            )

            sub_env = dict(os.environ)
            sub_env["HERMES_HOME"] = str(isolated_home)
            sub_env["NO_PROXY"] = os.environ["NO_PROXY"]
            sub_env["no_proxy"] = os.environ["no_proxy"]
            for k, v in secret_env.items():
                sub_env[k] = v

            proc = subprocess.run(
                [
                    hermes_bin,
                    "chat",
                    "-q",
                    prompt,
                    "--oneshot",
                    "-Q",
                    "-t",
                    "mcp-open_ontology",
                    "--max-turns",
                    "16",
                ],
                cwd=str(REPO_ROOT),
                env=sub_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=180,
            )

        elapsed = round(time.monotonic() - t0, 2)
        if proc.returncode != 0:
            stderr_log = isolated_home / "logs" / "mcp-stderr.log"
            extra = stderr_log.read_text(encoding="utf-8", errors="ignore")[-1500:] if stderr_log.is_file() else ""
            raise RuntimeError(
                f"hermes exited with rc={proc.returncode}\nstdout={proc.stdout[-1500:]}\nstderr={proc.stderr[-1500:]}\nmcp_log={extra}"
            )

        admin = actors["admin"]
        ord_safe = rt.get_object("O2cOrder", "ORD-SAFE-101", admin)
        ord_risk = rt.get_object("O2cOrder", "ORD-RISK-102", admin)
        tk_risk = rt.get_object("CrmTicket", "TK-URG-901", admin)
        work_cards = rt.query_objects("WorkCard", admin)

        # Deterministic ABox verification
        assert len(mcp_srv.call_history) >= 4, f"too few MCP calls: {mcp_srv.call_history}"
        assert ord_safe.status == "confirmed", f"expected ORD-SAFE-101 confirmed, got {ord_safe.status}"
        assert ord_risk.status == "draft", f"expected ORD-RISK-102 draft, got {ord_risk.status}"
        assert tk_risk.status == "OPEN", f"expected TK-URG-901 OPEN, got {tk_risk.status}"
        matching_cards = [
            wc
            for wc in work_cards
            if wc.status == "OPEN"
            and wc.target_object_type == "O2cOrder"
            and wc.target_object_id == "ORD-RISK-102"
            and wc.requester_actor_id == "rep-lin"
        ]
        assert len(matching_cards) >= 1, f"expected OPEN WorkCard by rep-lin for ORD-RISK-102, got {work_cards}"

        report = {
            "hermes_version": hermes_version,
            "mcp_transport": "Streamable HTTP (POST /mcp)",
            "provider": channel["provider"],
            "model": channel["model"],
            "channel_name": channel["name"],
            "elapsed_sec": elapsed,
            "discovered_tools": [t["name"] for t in mcp_srv.list_tools()],
            "call_trajectory": [
                {
                    "tool": item["tool"],
                    "is_error": item["is_error"],
                    "error_code": item.get("error_code"),
                    "arguments": item["arguments"],
                }
                for item in mcp_srv.call_history
            ],
            "final_abox": {
                "ORD-SAFE-101": ord_safe.status,
                "ORD-RISK-102": ord_risk.status,
                "TK-URG-901": tk_risk.status,
                "open_work_cards": [
                    {
                        "card_id": wc.card_id,
                        "card_kind": wc.card_kind,
                        "target_object_type": wc.target_object_type,
                        "target_object_id": wc.target_object_id,
                        "requested_action": wc.requested_action,
                        "requester_actor_id": wc.requester_actor_id,
                        "assignee_role": wc.assignee_role,
                        "status": wc.status,
                    }
                    for wc in matching_cards
                ],
            },
            "agent_reply": proc.stdout.strip(),
        }
        return report
    finally:
        rt.close()
        for _ in range(5):
            shutil.rmtree(isolated_home, ignore_errors=True)
            if not isolated_home.exists():
                break
            time.sleep(0.2)


if __name__ == "__main__":  # pragma: no cover
    out = run_live_demo()
    print(json.dumps(out, ensure_ascii=False, indent=2))
