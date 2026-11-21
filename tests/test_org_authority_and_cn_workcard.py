"""Dual-backend (SQLite + PG16) tests for WO-23 org hierarchy, tiered authority & IM callbacks."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import time
from typing import Iterator
import urllib.error
import urllib.request
import uuid

import psycopg
import pytest

from open_ontology import Actor, InvariantError, OntologyRuntime
from open_ontology.mcp_server import OntologyMCPServer, start_mcp_http_server
from open_ontology.org_authority import (
    AUTH_TOKEN_INVALID,
    AUTH_TOKEN_MISSING,
    AUTHORITY_ORG_SCOPE_DENIED,
    AUTHORITY_TIER_INSUFFICIENT,
    ORG_CYCLE_DETECTED,
    ORG_MEMBER_INACTIVE,
    ORG_PARENT_NOT_FOUND,
    SOD_SELF_APPROVAL_FORBIDDEN,
    AuthorityTier,
    OrgDirectory,
    OrgAuthorityError,
    OrgMember,
    OrgUnit,
    TieredAuthorityPolicy,
    TieredAutonomyAdapter,
    hash_auth_token,
)
from open_ontology.postgres_backend import open_postgres_runtime

from examples.cn_org_workcard_bridge import (
    CALLBACK_REPLAY_DETECTED,
    CALLBACK_SIGNATURE_INVALID,
    CALLBACK_TIMESTAMP_EXPIRED,
    CallbackSecurityError,
    CnOrgWorkCardBridge,
    build_org_scoped_p2p_spec,
    scope_value_from_tag,
    sign_callback,
    sync_feishu_org_directory,
    sync_wecom_org_directory,
)

PG_DSN = os.environ.get(
    "OPEN_ONTOLOGY_PG_DSN",
    "postgresql://open_ontology:open_ontology_dev@127.0.0.1:15432/open_ontology",
)


def _sample_wecom_directory() -> tuple[OrgDirectory, dict[str, str]]:
    tokens = {
        "u_cfo": "tok-cfo-group-001",
        "u_p1_mgr": "tok-p1-mgr-002",
        "u_p1_buyer": "tok-p1-buyer-003",
        "u_p2_mgr": "tok-p2-mgr-004",
        "u_p2_buyer": "tok-p2-buyer-005",
        "u_resigned": "tok-resigned-006",
    }
    depts_payload = {
        "department": [
            {"id": 100, "name": "华北特钢集团", "parentid": 0, "unit_kind": "group"},
            {"id": 110, "name": "一分厂", "parentid": 100, "unit_kind": "plant"},
            {"id": 111, "name": "一分厂采购组", "parentid": 110, "unit_kind": "team"},
            {"id": 120, "name": "二分厂", "parentid": 100, "unit_kind": "plant"},
            {"id": 121, "name": "二分厂采购组", "parentid": 120, "unit_kind": "team"},
        ]
    }
    users_payload = {
        "userlist": [
            {"userid": "u_cfo", "name": "陈总监", "department": [100], "position": "集团财务总监", "status": 1},
            {"userid": "u_p1_mgr", "name": "王部长", "department": [110], "position": "一分厂采购部长", "status": 1},
            {"userid": "u_p1_buyer", "name": "张采购", "department": [111], "position": "采购专员", "status": 1},
            {"userid": "u_p2_mgr", "name": "李部长", "department": [120], "position": "二分厂采购部长", "status": 1},
            {"userid": "u_p2_buyer", "name": "赵采购", "department": [121], "position": "采购专员", "status": 1},
            {"userid": "u_resigned", "name": "周离职", "department": [111], "position": "采购专员", "status": 4},
        ]
    }
    directory = sync_wecom_org_directory(
        depts_payload,
        users_payload,
        role_mapping={
            "财务总监": "finance_director",
            "采购部长": "procurement_manager",
            "采购专员": "buyer",
        },
        token_hashes={uid: hash_auth_token(tok) for uid, tok in tokens.items()},
    )
    return directory, tokens


def _default_policy() -> TieredAuthorityPolicy:
    return TieredAuthorityPolicy(
        tiers=(
            AuthorityTier(
                tier_id="TIER_1_AUTO",
                min_amount=0.0,
                max_amount=100_000.0,
                mode="AUTO",
                required_approver_role=None,
            ),
            AuthorityTier(
                tier_id="TIER_2_PLANT_MGR",
                min_amount=100_000.01,
                max_amount=500_000.0,
                mode="ESCALATE",
                required_approver_role="procurement_manager",
                require_covering_org_scope=True,
            ),
            AuthorityTier(
                tier_id="TIER_3_GROUP_CFO",
                min_amount=500_000.01,
                max_amount=None,
                mode="ESCALATE",
                required_approver_role="finance_director",
                require_covering_org_scope=True,
            ),
        )
    )


@contextmanager
def _open_backend_runtime(backend: str, spec: Any) -> Iterator[OntologyRuntime]:
    if backend == "sqlite":
        rt = OntologyRuntime(spec, db_path=":memory:")
        try:
            yield rt
        finally:
            rt.close()
        return

    schema = f"wo23_{uuid.uuid4().hex[:8]}"
    rt = open_postgres_runtime(spec, PG_DSN, schema_name=schema, world_mode="standalone")
    try:
        yield rt
    finally:
        rt.close()
        with psycopg.connect(PG_DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            rem = conn.execute(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            ).fetchall()
            assert rem == [], f"PG16 schema leaked: {rem}"


def _seed_plant_orders(rt: OntologyRuntime, directory: OrgDirectory) -> None:
    admin = Actor(actor_id="seed-admin", role="admin", clearance_tags={"*"})
    ent_cls = rt.spec.get_object_type("AccountingEntity")
    pty_cls = rt.spec.get_object_type("BusinessParty")
    sup_cls = rt.spec.get_object_type("P2pSupplierProfile")
    po_cls = rt.spec.get_object_type("P2pPurchaseOrder")

    rt.put_object(ent_cls(entity_id="ENT-HB", name="华北特钢集团", status="active"), actor=admin)
    rt.put_object(pty_cls(party_id="PTY-SUP", legal_name="唐山矿粉供应商", status="active"), actor=admin)
    rt.put_object(
        sup_cls(profile_id="SUP-1", party="PTY-SUP", accounting_entity="ENT-HB", status="active"),
        actor=admin,
    )

    p1_org = scope_value_from_tag(directory.get_unit("111").scope_tag)
    p2_org = scope_value_from_tag(directory.get_unit("121").scope_tag)
    orders = [
        ("PO-P1-80K", "PO-NUM-080", p1_org, 80.0, 1000.0, 80_000.0),
        ("PO-P1-250K", "PO-NUM-250", p1_org, 250.0, 1000.0, 250_000.0),
        ("PO-P1-800K", "PO-NUM-800", p1_org, 800.0, 1000.0, 800_000.0),
        ("PO-P2-90K", "PO-NUM-P2-090", p2_org, 90.0, 1000.0, 90_000.0),
    ]
    for oid, num, org_val, qty, px, amt in orders:
        rt.put_object(
            po_cls(
                po_id=oid,
                po_number=num,
                supplier="SUP-1",
                accounting_entity="ENT-HB",
                ordered_qty=qty,
                unit_price=px,
                amount_total=amt,
                status="draft",
                org=org_val,
            ),
            actor=admin,
        )


def _mcp_post(mcp_url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        mcp_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=hdrs,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_wecom_and_feishu_org_tree_sync_and_cycle_guards() -> None:
    wecom_dir, _ = _sample_wecom_directory()
    feishu_dir = sync_feishu_org_directory(
        {
            "data": {
                "items": [
                    {"open_department_id": "100", "name": "华北特钢集团", "parent_department_id": "0"},
                    {"open_department_id": "110", "name": "一分厂", "parent_department_id": "100"},
                    {"open_department_id": "111", "name": "一分厂采购组", "parent_department_id": "110"},
                    {"open_department_id": "120", "name": "二分厂", "parent_department_id": "100"},
                    {"open_department_id": "121", "name": "二分厂采购组", "parent_department_id": "120"},
                ]
            }
        },
        {
            "data": {
                "items": [
                    {"open_id": "u_cfo", "name": "陈总监", "department_ids": ["100"], "job_title": "集团财务总监", "status": {"is_activated": True, "is_resigned": False}},
                    {"open_id": "u_p1_mgr", "name": "王部长", "department_ids": ["110"], "job_title": "一分厂采购部长", "status": {"is_activated": True, "is_resigned": False}},
                    {"open_id": "u_p1_buyer", "name": "张采购", "department_ids": ["111"], "job_title": "采购专员", "status": {"is_activated": True, "is_resigned": False}},
                    {"open_id": "u_p2_mgr", "name": "李部长", "department_ids": ["120"], "job_title": "二分厂采购部长", "status": {"is_activated": True, "is_resigned": False}},
                    {"open_id": "u_resigned", "name": "周离职", "department_ids": ["111"], "job_title": "采购专员", "status": {"is_activated": True, "is_resigned": True}},
                ]
            }
        },
        role_mapping={
            "财务总监": "finance_director",
            "采购部长": "procurement_manager",
            "采购专员": "buyer",
        },
    )

    for directory in (wecom_dir, feishu_dir):
        assert directory.ancestors("111") == ["110", "100"]
        assert directory.subtree_ids("110") == {"110", "111"}
        assert directory.covers("100", "111") is True
        assert directory.covers("100", "121") is True
        assert directory.covers("110", "111") is True
        assert directory.covers("110", "121") is False
        assert directory.covers("120", "111") is False

        p1_buyer_actor = directory.resolve_actor("u_p1_buyer")
        assert p1_buyer_actor.role == "buyer"
        assert "org:dept-111" in p1_buyer_actor.clearance_tags
        assert "org:dept-121" not in p1_buyer_actor.clearance_tags

        with pytest.raises(OrgAuthorityError) as exc_inactive:
            directory.resolve_actor("u_resigned")
        assert exc_inactive.value.code == ORG_MEMBER_INACTIVE

    with pytest.raises(InvariantError) as exc_cycle:
        OrgDirectory(
            units=[
                OrgUnit(unit_id="A", name="A", parent_id="B", scope_tag="org:a"),
                OrgUnit(unit_id="B", name="B", parent_id="A", scope_tag="org:b"),
            ]
        )
    assert exc_cycle.value.code == ORG_CYCLE_DETECTED

    with pytest.raises(InvariantError) as exc_dangling:
        OrgDirectory(
            units=[OrgUnit(unit_id="A", name="A", parent_id="MISSING_ROOT", scope_tag="org:a")]
        )
    assert exc_dangling.value.code == ORG_PARENT_NOT_FOUND


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_plant_rls_isolation_and_mcp_bearer_auth(backend: str) -> None:
    directory, tokens = _sample_wecom_directory()
    spec = build_org_scoped_p2p_spec()

    with _open_backend_runtime(backend, spec) as rt:
        _seed_plant_orders(rt, directory)
        mcp = OntologyMCPServer(
            rt,
            Actor(actor_id="unprivileged-fallback", role="system"),
            actor_resolver=directory.build_mcp_actor_resolver(),
        )
        with start_mcp_http_server(mcp, host="127.0.0.1", port=0) as hdl:
            # 1. Missing / wrong / resigned tokens fail closed even with spoofed X-Role: admin
            for hdrs, expected_code in (
                ({"X-Role": "admin"}, AUTH_TOKEN_MISSING),
                ({"Authorization": "Bearer wrong-token", "X-Role": "admin"}, AUTH_TOKEN_INVALID),
                ({"Authorization": f"Bearer {tokens['u_resigned']}", "X-Role": "admin"}, ORG_MEMBER_INACTIVE),
            ):
                _st, resp = _mcp_post(
                    hdl.mcp_url,
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "query_objects", "arguments": {"object_type": "P2pPurchaseOrder"}}},
                    headers=hdrs,
                )
                assert resp["error"]["data"]["code"] == expected_code

            # 2. Plant 1 buyer sees ONLY Plant 1 orders; Plant 2 order is physically invisible & blocked
            p1_hdr = {"Authorization": f"Bearer {tokens['u_p1_buyer']}", "X-Role": "admin"}
            _st, q_resp = _mcp_post(
                hdl.mcp_url,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query_objects", "arguments": {"object_type": "P2pPurchaseOrder"}}},
                headers=p1_hdr,
            )
            q_payload = json.loads(q_resp["result"]["content"][0]["text"])
            visible_ids = {row["po_id"] for row in q_payload["objects"]}
            assert visible_ids == {"PO-P1-80K", "PO-P1-250K", "PO-P1-800K"}
            assert "PO-P2-90K" not in visible_ids

            _st, get_p2_resp = _mcp_post(
                hdl.mcp_url,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "get_object",
                        "arguments": {
                            "object_type": "P2pPurchaseOrder",
                            "object_id": "PO-P2-90K",
                        },
                    },
                },
                headers=p1_hdr,
            )
            assert get_p2_resp["result"]["isError"] is True

            _st, cross_resp = _mcp_post(
                hdl.mcp_url,
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "submit_action",
                        "arguments": {
                            "run_id": "MCP-CROSS-P2",
                            "action": "confirm_p2p_order",
                            "params": {"po_id": "PO-P2-90K"},
                        },
                    },
                },
                headers=p1_hdr,
            )
            assert cross_resp["result"]["isError"] is True
            assert rt.get_object("P2pPurchaseOrder", "PO-P2-90K", directory.resolve_actor("u_p2_buyer")).status == "draft"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_three_tier_amount_authority_and_wecom_feishu_callbacks(backend: str) -> None:
    directory, _ = _sample_wecom_directory()
    policy = _default_policy()
    spec = build_org_scoped_p2p_spec()
    secret = "wo23-callback-hmac-secret"

    with _open_backend_runtime(backend, spec) as rt:
        _seed_plant_orders(rt, directory)
        bridge = CnOrgWorkCardBridge(runtime=rt, directory=directory, policy=policy)
        cfo_actor = directory.resolve_actor("u_cfo")

        # 0. Cross-plant submission denied before any runtime call
        with pytest.raises(OrgAuthorityError) as exc_cross_submit:
            bridge.submit_or_escalate_with_org_authority(
                requester_user_id="u_p2_buyer",
                target_unit_id="111",
                run_id="CROSS-SUB-1",
                action="confirm_p2p_order",
                params={"po_id": "PO-P1-80K"},
                target_object_type="P2pPurchaseOrder",
                target_object_id="PO-P1-80K",
                amount=80_000.0,
            )
        assert exc_cross_submit.value.code == AUTHORITY_ORG_SCOPE_DENIED

        # 1. Tier 1 (80,000 CNY <= 100,000): AUTO commit
        res_80k = bridge.submit_or_escalate_with_org_authority(
            requester_user_id="u_p1_buyer",
            target_unit_id="111",
            run_id="RUN-80K",
            action="confirm_p2p_order",
            params={"po_id": "PO-P1-80K"},
            target_object_type="P2pPurchaseOrder",
            target_object_id="PO-P1-80K",
            amount=80_000.0,
        )
        assert res_80k["outcome"] == "AUTO_COMMITTED"
        assert rt.get_object("P2pPurchaseOrder", "PO-P1-80K", cfo_actor).status == "confirmed"

        # 2. Tier 2 (250,000 CNY): ESCALATE to Plant 1 procurement_manager
        res_250k = bridge.submit_or_escalate_with_org_authority(
            requester_user_id="u_p1_buyer",
            target_unit_id="111",
            run_id="RUN-250K",
            action="confirm_p2p_order",
            params={"po_id": "PO-P1-250K"},
            target_object_type="P2pPurchaseOrder",
            target_object_id="PO-P1-250K",
            amount=250_000.0,
            card_id="WC-P1-250K",
        )
        assert res_250k["outcome"] == "ESCALATED_WORK_CARD"
        assert res_250k["evaluation"]["eligible_approver_ids"] == ["u_p1_mgr"]
        assert res_250k["wecom_card"]["template_card"]["task_id"] == "WC-P1-250K"
        assert res_250k["feishu_card"]["schema"] == "2.0"

        now_ts = int(time.time())
        # 2a. Plant 2 manager hijack attempt -> AUTHORITY_ORG_SCOPE_DENIED
        bad_p2_bytes = json.dumps({"userid": "u_p2_mgr", "EventKey": "APPROVE:WC-P1-250K:TIER_2_PLANT_MGR"}).encode("utf-8")
        with pytest.raises(OrgAuthorityError) as exc_p2:
            bridge.handle_card_callback(
                "wecom",
                bad_p2_bytes,
                signature=sign_callback(bad_p2_bytes, now_ts, "n-p2-hijack", secret),
                timestamp=now_ts,
                nonce="n-p2-hijack",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_p2.value.code == AUTHORITY_ORG_SCOPE_DENIED

        # 2b. Self-approval attempt by requester -> SOD_SELF_APPROVAL_FORBIDDEN
        self_bytes = json.dumps({"userid": "u_p1_buyer", "EventKey": "APPROVE:WC-P1-250K:TIER_2_PLANT_MGR"}).encode("utf-8")
        with pytest.raises(OrgAuthorityError) as exc_sod:
            bridge.handle_card_callback(
                "wecom",
                self_bytes,
                signature=sign_callback(self_bytes, now_ts, "n-self-250", secret),
                timestamp=now_ts,
                nonce="n-self-250",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_sod.value.code == SOD_SELF_APPROVAL_FORBIDDEN

        # 2c. Legitimate Plant 1 manager WeCom approval -> atomically confirms PO + completes WorkCard
        ok_p1_bytes = json.dumps({"userid": "u_p1_mgr", "EventKey": "APPROVE:WC-P1-250K:TIER_2_PLANT_MGR"}).encode("utf-8")
        cb_250k = bridge.handle_card_callback(
            "wecom",
            ok_p1_bytes,
            signature=sign_callback(ok_p1_bytes, now_ts, "n-ok-250", secret),
            timestamp=now_ts,
            nonce="n-ok-250",
            secret=secret,
            now_ts=now_ts,
        )
        assert cb_250k["outcome"] == "APPROVED_AND_SETTLED"
        assert rt.get_object("P2pPurchaseOrder", "PO-P1-250K", cfo_actor).status == "confirmed"
        assert rt.get_object("WorkCard", "WC-P1-250K", cfo_actor).status == "COMPLETED"

        # 3. Tier 3 (800,000 CNY): ESCALATE to Group finance_director (u_cfo)
        res_800k = bridge.submit_or_escalate_with_org_authority(
            requester_user_id="u_p1_buyer",
            target_unit_id="111",
            run_id="RUN-800K",
            action="confirm_p2p_order",
            params={"po_id": "PO-P1-800K"},
            target_object_type="P2pPurchaseOrder",
            target_object_id="PO-P1-800K",
            amount=800_000.0,
            card_id="WC-P1-800K",
        )
        assert res_800k["evaluation"]["eligible_approver_ids"] == ["u_cfo"]

        # 3a. Plant 1 manager tries to exceed authority -> AUTHORITY_TIER_INSUFFICIENT
        over_tier_bytes = json.dumps({"open_id": "u_p1_mgr", "action": {"value": {"card_id": "WC-P1-800K", "decision": "APPROVE"}}}).encode("utf-8")
        with pytest.raises(OrgAuthorityError) as exc_tier:
            bridge.handle_card_callback(
                "feishu",
                over_tier_bytes,
                signature=sign_callback(over_tier_bytes, now_ts, "n-over-800", secret),
                timestamp=now_ts,
                nonce="n-over-800",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_tier.value.code == AUTHORITY_TIER_INSUFFICIENT

        # 3b. Group CFO approves via Feishu callback
        ok_cfo_bytes = json.dumps({"open_id": "u_cfo", "action": {"value": {"card_id": "WC-P1-800K", "decision": "APPROVE"}}}).encode("utf-8")
        cb_800k = bridge.handle_card_callback(
            "feishu",
            ok_cfo_bytes,
            signature=sign_callback(ok_cfo_bytes, now_ts, "n-ok-800", secret),
            timestamp=now_ts,
            nonce="n-ok-800",
            secret=secret,
            now_ts=now_ts,
        )
        assert cb_800k["outcome"] == "APPROVED_AND_SETTLED"
        assert rt.get_object("P2pPurchaseOrder", "PO-P1-800K", cfo_actor).status == "confirmed"
        assert rt.get_object("WorkCard", "WC-P1-800K", cfo_actor).status == "COMPLETED"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_callback_security_replay_and_t1_phantom_read_defense(backend: str) -> None:
    directory, _ = _sample_wecom_directory()
    policy = _default_policy()
    spec = build_org_scoped_p2p_spec()
    secret = "wo23-security-secret"

    with _open_backend_runtime(backend, spec) as rt:
        _seed_plant_orders(rt, directory)
        bridge = CnOrgWorkCardBridge(runtime=rt, directory=directory, policy=policy)
        cfo_actor = directory.resolve_actor("u_cfo")

        # 1. TieredAutonomyAdapter: SHADOW mode records zero writes
        adapter = TieredAutonomyAdapter(directory, policy, escalation_action="open_work_card", shadow_only=True)
        _ev_sh, dec_sh = adapter.execute_with_authority(
            rt,
            requester_user_id="u_p1_buyer",
            target_unit_id="111",
            run_id="ADAPT-SHADOW",
            action="confirm_p2p_order",
            params={"po_id": "PO-P1-80K"},
            amount=80_000.0,
        )
        assert dec_sh.outcome == "SHADOW_RECORDED"
        assert rt.get_object("P2pPurchaseOrder", "PO-P1-80K", cfo_actor).status == "draft"

        # 2. Issue 800k escalation card at t0
        bridge.submit_or_escalate_with_org_authority(
            requester_user_id="u_p1_buyer",
            target_unit_id="111",
            run_id="RUN-PHANTOM",
            action="confirm_p2p_order",
            params={"po_id": "PO-P1-800K"},
            target_object_type="P2pPurchaseOrder",
            target_object_id="PO-P1-800K",
            amount=800_000.0,
            card_id="WC-PHANTOM-800K",
        )

        now_ts = int(time.time())
        cfo_payload = json.dumps({"open_id": "u_cfo", "action": {"value": {"card_id": "WC-PHANTOM-800K", "decision": "APPROVE"}}}).encode("utf-8")

        # 2a. Forged HMAC signature -> CALLBACK_SIGNATURE_INVALID
        with pytest.raises(CallbackSecurityError) as exc_sig:
            bridge.handle_card_callback(
                "feishu",
                cfo_payload,
                signature="0" * 64,
                timestamp=now_ts,
                nonce="n-bad-sig",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_sig.value.code == CALLBACK_SIGNATURE_INVALID

        # 2b. Expired timestamp -> CALLBACK_TIMESTAMP_EXPIRED
        old_ts = now_ts - 900
        with pytest.raises(CallbackSecurityError) as exc_exp:
            bridge.handle_card_callback(
                "feishu",
                cfo_payload,
                signature=sign_callback(cfo_payload, old_ts, "n-old", secret),
                timestamp=old_ts,
                nonce="n-old",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_exp.value.code == CALLBACK_TIMESTAMP_EXPIRED

        # 2c. Mutate PO between t0 and t1 (cancelled) -> t1 live re-simulation blocks with PO_NOT_DRAFT
        po_cls = rt.spec.get_object_type("P2pPurchaseOrder")
        p1_org = scope_value_from_tag(directory.get_unit("111").scope_tag)
        rt.put_object(
            po_cls(
                po_id="PO-P1-800K",
                po_number="PO-NUM-800",
                supplier="SUP-1",
                accounting_entity="ENT-HB",
                ordered_qty=800.0,
                unit_price=1000.0,
                amount_total=800_000.0,
                status="cancelled",
                org=p1_org,
            ),
            actor=cfo_actor,
        )
        sig_t1 = sign_callback(cfo_payload, now_ts, "n-replay-t1", secret)
        with pytest.raises(InvariantError) as exc_t1:
            bridge.handle_card_callback(
                "feishu",
                cfo_payload,
                signature=sig_t1,
                timestamp=now_ts,
                nonce="n-replay-t1",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_t1.value.code == "PO_NOT_DRAFT"
        assert rt.get_object("WorkCard", "WC-PHANTOM-800K", cfo_actor).status == "OPEN"
        assert rt.get_object("P2pPurchaseOrder", "PO-P1-800K", cfo_actor).status == "cancelled"

        # 2d. Replaying same nonce -> CALLBACK_REPLAY_DETECTED
        with pytest.raises(CallbackSecurityError) as exc_rep:
            bridge.handle_card_callback(
                "feishu",
                cfo_payload,
                signature=sig_t1,
                timestamp=now_ts,
                nonce="n-replay-t1",
                secret=secret,
                now_ts=now_ts,
            )
        assert exc_rep.value.code == CALLBACK_REPLAY_DETECTED

        # 2e. Valid REJECT callback cleanly marks WorkCard REJECTED
        rej_payload = json.dumps({"open_id": "u_cfo", "action": {"value": {"card_id": "WC-PHANTOM-800K", "decision": "REJECT"}}}).encode("utf-8")
        rej_res = bridge.handle_card_callback(
            "feishu",
            rej_payload,
            signature=sign_callback(rej_payload, now_ts, "n-reject-ok", secret),
            timestamp=now_ts,
            nonce="n-reject-ok",
            secret=secret,
            now_ts=now_ts,
        )
        assert rej_res["outcome"] == "REJECTED_WORK_CARD"
        assert rt.get_object("WorkCard", "WC-PHANTOM-800K", cfo_actor).status == "REJECTED"
