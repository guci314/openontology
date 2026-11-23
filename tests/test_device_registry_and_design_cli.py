"""WO-11 离线确定性回归套件：DeviceDriverRegistry、Workshop 实时连接器、Design CLI 与隔离泥潭双轮核验。"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from examples.agy_fde_mudpit_decompile_benchmark import (
    BOTTLECRM_SOURCE_DB,
    _independent_referee_audit,
    _sha256,
    build_locked_enterprise_target_blueprint,
    build_mudpit_crm_and_erp_sqlite_pair,
    build_reference_crm_mapping,
    build_reference_customer_overlay,
    build_reference_entity_resolution_spec,
    build_reference_erp_mapping,
    prepare_isolated_player_workspace,
)
from open_ontology import (
    CROSS_DEVICE_WRITE_FORBIDDEN,
    DUPLICATE_ACTION_DRIVER,
    DUPLICATE_DEVICE_ID,
    DUPLICATE_OWNED_OBJECT_DRIVER,
    Actor,
    DeviceDriverRegistry,
    DeviceStatus,
    GovernanceViolationError,
    InvariantError,
    MountedDevice,
    ObjectType,
    OntologyRuntime,
    OntologySpec,
    SourceBridge,
)
from open_ontology.settlement import UNBOUND_SOURCE_BRIDGE
from open_ontology.workshop import CommandCenterRegistry, _instance_summary, start_workshop_server


# ---------------------------------------------------------------------------
# Helpers & Pure M2 Multi-Device Fixtures (Zero LLM)
# ---------------------------------------------------------------------------
def _make_dual_device_spec() -> tuple[OntologySpec, type[ObjectType], type[ObjectType], type[ObjectType]]:
    spec = OntologySpec()

    @spec.object_type
    class SharedAccount(ObjectType):
        __primary_key__: ClassVar[str] = "account_id"
        __title__: ClassVar[str] = "legal_name"

        account_id: str
        legal_name: str = ""
        tax_id: str = ""
        country: str = ""

    @spec.object_type
    class DevAItem(ObjectType):
        __primary_key__: ClassVar[str] = "item_id"
        __title__: ClassVar[str] = "item_id"

        item_id: str
        status: str = "init"

    @spec.object_type
    class DevBItem(ObjectType):
        __primary_key__: ClassVar[str] = "item_id"
        __title__: ClassVar[str] = "item_id"

        item_id: str
        status: str = "init"

    class UpdateOneParams(BaseModel):
        item_id: str
        status: str = "done"

    class CrossWriteParams(BaseModel):
        a_id: str
        b_id: str

    @spec.edit_function("update_a", UpdateOneParams, touched_object_types=("DevAItem",))
    def update_a(ctx: Any, params: UpdateOneParams) -> None:
        item = ctx.require(DevAItem, params.item_id)
        ctx.modify(item, status=params.status)

    spec.declare_action("update_a", edit_function="update_a", allowed_roles=("ops", "admin"))

    @spec.edit_function("update_b", UpdateOneParams, touched_object_types=("DevBItem",))
    def update_b(ctx: Any, params: UpdateOneParams) -> None:
        item = ctx.require(DevBItem, params.item_id)
        ctx.modify(item, status=params.status)

    spec.declare_action("update_b", edit_function="update_b", allowed_roles=("ops", "admin"))

    @spec.edit_function(
        "cross_write_a_and_b",
        CrossWriteParams,
        touched_object_types=("DevAItem", "DevBItem"),
    )
    def cross_write_a_and_b(ctx: Any, params: CrossWriteParams) -> None:
        a = ctx.require(DevAItem, params.a_id)
        b = ctx.require(DevBItem, params.b_id)
        ctx.modify(a, status="mutated")
        ctx.modify(b, status="mutated")

    spec.declare_action(
        "cross_write_a_and_b",
        edit_function="cross_write_a_and_b",
        allowed_roles=("ops", "admin"),
    )

    @spec.edit_function(
        "rogue_a_touches_b",
        CrossWriteParams,
        touched_object_types=("DevBItem",),
    )
    def rogue_a_touches_b(ctx: Any, params: CrossWriteParams) -> None:
        b = ctx.require(DevBItem, params.b_id)
        ctx.modify(b, status="hijacked")

    spec.declare_action(
        "rogue_a_touches_b",
        edit_function="rogue_a_touches_b",
        allowed_roles=("ops", "admin"),
    )

    return spec, SharedAccount, DevAItem, DevBItem


def _make_memory_bridge(
    *,
    shared_rows: list[dict[str, Any]] | None = None,
    owned_type: str,
    owned_rows: dict[str, dict[str, Any]],
    write_log: list[dict[str, Any]],
) -> SourceBridge:
    shared_list = [dict(r) for r in (shared_rows or [])]

    def _actuate(move: dict[str, Any]) -> None:
        write_log.append(move)
        for ch in (move.get("object_diff") or {}).get("object_changes") or []:
            if ch.get("object_type") == owned_type:
                oid = str(ch["object_id"])
                owned_rows[oid] = dict(ch.get("after") or owned_rows[oid])

    def _observe(move: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        if move is None:
            out: dict[str, list[dict[str, Any]]] = {owned_type: [dict(v) for v in owned_rows.values()]}
            if shared_list:
                out["SharedAccount"] = [dict(r) for r in shared_list]
            return out
        return {owned_type: [dict(v) for v in owned_rows.values()]}

    return SourceBridge(actuate=_actuate, observe=_observe)


def _run_design_cli(args: list[str]) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-m", "open_ontology.design", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    return proc.returncode, payload


# ---------------------------------------------------------------------------
# 1. DeviceDriverRegistry & Workshop Live Connector Tests
# ---------------------------------------------------------------------------
def test_device_driver_registry_exports_and_mount_conflict_guards() -> None:
    status_hint: DeviceStatus = "MOUNTED"
    assert status_hint == "MOUNTED"
    spec, _shared, _a_cls, _b_cls = _make_dual_device_spec()
    dummy_bridge = SourceBridge(actuate=lambda _mv: None, observe=lambda _mv=None: {})

    # Same device declares object type as both owned and shared -> DUPLICATE_OWNED_OBJECT_DRIVER
    with pytest.raises(InvariantError) as same_overlap:
        MountedDevice(
            device_id="bad_self",
            name="Bad Self",
            kind="memory",
            endpoint="mem://bad",
            bridge=dummy_bridge,
            owned_object_types=("SharedAccount",),
            shared_object_types=("SharedAccount",),
        )
    assert same_overlap.value.code == DUPLICATE_OWNED_OBJECT_DRIVER

    bus = DeviceDriverRegistry(spec=spec)
    bus.mount(
        MountedDevice(
            device_id="dev_a",
            name="Device A",
            kind="memory",
            endpoint="mem://a",
            bridge=dummy_bridge,
            handled_actions=("update_a",),
            owned_object_types=("DevAItem",),
            shared_object_types=("SharedAccount",),
        )
    )

    # Duplicate device_id -> DUPLICATE_DEVICE_ID
    with pytest.raises(InvariantError) as dup_id:
        bus.mount(
            MountedDevice(
                device_id="dev_a",
                name="Duplicate A",
                kind="memory",
                endpoint="mem://a2",
                bridge=dummy_bridge,
                handled_actions=("update_b",),
                owned_object_types=("DevBItem",),
            )
        )
    assert dup_id.value.code == DUPLICATE_DEVICE_ID

    # Duplicate active handled_actions -> DUPLICATE_ACTION_DRIVER
    with pytest.raises(InvariantError) as dup_act:
        bus.mount(
            MountedDevice(
                device_id="dev_dup_act",
                name="Dup Act",
                kind="memory",
                endpoint="mem://da",
                bridge=dummy_bridge,
                handled_actions=("update_a",),
                owned_object_types=("DevBItem",),
            )
        )
    assert dup_act.value.code == DUPLICATE_ACTION_DRIVER

    # Owned vs owned conflict -> DUPLICATE_OWNED_OBJECT_DRIVER
    with pytest.raises(InvariantError) as dup_owned:
        bus.mount(
            MountedDevice(
                device_id="dev_dup_owned",
                name="Dup Owned",
                kind="memory",
                endpoint="mem://do",
                bridge=dummy_bridge,
                handled_actions=("update_b",),
                owned_object_types=("DevAItem",),
            )
        )
    assert dup_owned.value.code == DUPLICATE_OWNED_OBJECT_DRIVER

    # Owned vs existing shared conflict -> DUPLICATE_OWNED_OBJECT_DRIVER
    with pytest.raises(InvariantError) as dup_shared_owned:
        bus.mount(
            MountedDevice(
                device_id="dev_own_shared",
                name="Own Shared",
                kind="memory",
                endpoint="mem://so",
                bridge=dummy_bridge,
                handled_actions=("update_b",),
                owned_object_types=("SharedAccount",),
            )
        )
    assert dup_shared_owned.value.code == DUPLICATE_OWNED_OBJECT_DRIVER


def test_device_driver_registry_unmount_degraded_replace_cross_write_and_merge() -> None:
    spec, shared_cls, a_cls, b_cls = _make_dual_device_spec()
    writes_a: list[dict[str, Any]] = []
    writes_b: list[dict[str, Any]] = []
    writes_b_v2: list[dict[str, Any]] = []
    rows_a = {"A-1": {"item_id": "A-1", "status": "init"}}
    rows_b = {"B-1": {"item_id": "B-1", "status": "init"}}

    bridge_a = _make_memory_bridge(
        shared_rows=[{"account_id": "PTY-1", "legal_name": "Acme Corp", "tax_id": "", "country": "CN"}],
        owned_type="DevAItem",
        owned_rows=rows_a,
        write_log=writes_a,
    )
    bridge_b = _make_memory_bridge(
        shared_rows=[{"account_id": "PTY-1", "legal_name": "Acme Ignored", "tax_id": "91310000ACME", "country": ""}],
        owned_type="DevBItem",
        owned_rows=rows_b,
        write_log=writes_b,
    )
    bridge_b_v2 = _make_memory_bridge(
        shared_rows=[{"account_id": "PTY-1", "legal_name": "Acme Ignored", "tax_id": "91310000ACME", "country": "CN"}],
        owned_type="DevBItem",
        owned_rows=rows_b,
        write_log=writes_b_v2,
    )

    bus = DeviceDriverRegistry(spec=spec)
    bus.mount(
        MountedDevice(
            device_id="dev_a",
            name="Dev A",
            kind="memory",
            endpoint="mem://a",
            bridge=bridge_a,
            handled_actions=("update_a", "cross_write_a_and_b", "rogue_a_touches_b"),
            owned_object_types=("DevAItem",),
            shared_object_types=("SharedAccount",),
        )
    )
    bus.mount(
        MountedDevice(
            device_id="dev_b",
            name="Dev B",
            kind="memory",
            endpoint="mem://b",
            bridge=bridge_b,
            handled_actions=("update_b",),
            owned_object_types=("DevBItem",),
            shared_object_types=("SharedAccount",),
        )
    )

    # 1. observe_all & observe PK-coalesced merging of shared_object_types
    snap = bus.observe_all()
    assert len(snap["SharedAccount"]) == 1
    assert snap["SharedAccount"][0] == {
        "account_id": "PTY-1",
        "legal_name": "Acme Corp",
        "tax_id": "91310000ACME",
        "country": "CN",
    }
    custom_snap = bus.observe_all(shared_mergers={"SharedAccount": lambda rows: [rows[-1]]})
    assert custom_snap["SharedAccount"][0]["legal_name"] == "Acme Ignored"

    rt = OntologyRuntime(spec, ":memory:", world_mode="coupled", source_bridge=bus.as_source_bridge())
    try:
        admin = Actor(actor_id="adm", role="admin", clearance_tags={"*"})
        ops = Actor(actor_id="ops-1", role="ops")
        rt.put_object(shared_cls(account_id="PTY-1", legal_name="Acme Corp", tax_id="91310000ACME", country="CN"), actor=admin)
        rt.put_object(a_cls(item_id="A-1", status="init"), actor=admin)
        rt.put_object(b_cls(item_id="B-1", status="init"), actor=admin)

        # 2. Single-action cross-device write red tests: fail closed with 0 partial writes on both devices
        with pytest.raises(GovernanceViolationError) as cross_err:
            rt.submit_action("TX-CROSS-1", "cross_write_a_and_b", {"a_id": "A-1", "b_id": "B-1"}, ops)
        assert cross_err.value.code == CROSS_DEVICE_WRITE_FORBIDDEN
        assert len(writes_a) == 0 and len(writes_b) == 0
        assert rt.get_object(a_cls, "A-1", admin).status == "init"
        assert rt.get_object(b_cls, "B-1", admin).status == "init"

        with pytest.raises(GovernanceViolationError) as rogue_err:
            rt.submit_action("TX-ROGUE-1", "rogue_a_touches_b", {"a_id": "A-1", "b_id": "B-1"}, ops)
        assert rogue_err.value.code == CROSS_DEVICE_WRITE_FORBIDDEN
        assert len(writes_a) == 0 and len(writes_b) == 0

        # 3. Normal single-device action commits & updates physical bridge B
        assert rt.submit_action("TX-B-1", "update_b", {"item_id": "B-1", "status": "step1"}, ops)["status"] == "COMMITTED"
        assert rows_b["B-1"]["status"] == "step1"
        assert len(writes_b) == 1

        # 4. replace() rollback on conflict preserves original dev_b intact
        with pytest.raises(InvariantError) as bad_rep:
            bus.replace(
                "dev_b",
                MountedDevice(
                    device_id="dev_b",
                    name="Conflict B",
                    kind="memory",
                    endpoint="mem://b_bad",
                    bridge=bridge_b_v2,
                    handled_actions=("update_a",),
                    owned_object_types=("DevBItem",),
                ),
            )
        assert bad_rep.value.code == DUPLICATE_ACTION_DRIVER
        assert bus.get_device("dev_b").endpoint == "mem://b"

        # 5. Atomic hot-swap replace() routes subsequent writes cleanly to bridge_b_v2
        bus.replace(
            "dev_b",
            MountedDevice(
                device_id="dev_b",
                name="Dev B v2",
                kind="memory",
                endpoint="mem://b_v2",
                bridge=bridge_b_v2,
                handled_actions=("update_b",),
                owned_object_types=("DevBItem",),
                shared_object_types=("SharedAccount",),
            ),
        )
        assert bus.get_device("dev_b").endpoint == "mem://b_v2"
        assert rt.submit_action("TX-B-2", "update_b", {"item_id": "B-1", "status": "step2"}, ops)["status"] == "COMMITTED"
        assert len(writes_b) == 1 and len(writes_b_v2) == 1
        assert rows_b["B-1"]["status"] == "step2"

        # 6. DEGRADED & unmount fail closed while sibling MOUNTED device keeps working
        bus.set_status("dev_a", "DEGRADED")
        with pytest.raises(GovernanceViolationError) as deg_err:
            rt.submit_action("TX-A-DEG", "update_a", {"item_id": "A-1", "status": "s1"}, ops)
        assert deg_err.value.code == UNBOUND_SOURCE_BRIDGE
        assert len(writes_a) == 0

        bus.set_status("dev_a", "MOUNTED")
        assert rt.submit_action("TX-A-OK", "update_a", {"item_id": "A-1", "status": "ok_a"}, ops)["status"] == "COMMITTED"
        assert len(writes_a) == 1

        bus.unmount("dev_a")
        with pytest.raises(GovernanceViolationError) as unm_err:
            rt.submit_action("TX-A-UNM", "update_a", {"item_id": "A-1", "status": "s2"}, ops)
        assert unm_err.value.code == UNBOUND_SOURCE_BRIDGE
        assert rt.submit_action("TX-B-3", "update_b", {"item_id": "B-1", "status": "step3"}, ops)["status"] == "COMMITTED"
        assert len(writes_b_v2) == 2
    finally:
        rt.close()


def test_workshop_live_driver_registry_connector_status_and_unplug() -> None:
    spec, _shared, _a_cls, _b_cls = _make_dual_device_spec()
    dummy_bridge = SourceBridge(actuate=lambda _mv: None, observe=lambda _mv=None: {})
    bus = DeviceDriverRegistry(spec=spec)
    bus.mount(
        MountedDevice(
            device_id="dev_crm_live",
            name="Live CRM",
            kind="sqlite",
            endpoint="sqlite://crm.db",
            bridge=dummy_bridge,
            handled_actions=("update_a",),
            owned_object_types=("DevAItem",),
        )
    )
    bus.mount(
        MountedDevice(
            device_id="dev_erp_live",
            name="Live ERP",
            kind="sqlite",
            endpoint="sqlite://erp.db",
            bridge=dummy_bridge,
            handled_actions=("update_b",),
            owned_object_types=("DevBItem",),
            status="DEGRADED",
        )
    )

    rt = OntologyRuntime(spec, ":memory:", world_mode="coupled", source_bridge=bus.as_source_bridge())
    reg = CommandCenterRegistry()
    reg.register_ontology("dual_dev", "Dual Devices", spec)
    unplug_calls = 0

    def _on_unplug() -> dict[str, Any]:
        nonlocal unplug_calls
        unplug_calls += 1
        return {"mode": "sandbox_only"}

    try:
        entry = reg.register_instance(
            instance_id="inst-drivers",
            ontology_id="dual_dev",
            label="Dual Drivers",
            runtime=rt,
            driver_registry=bus,
            unplug_fn=_on_unplug,
        )
        statuses = {c.name: c.status for c in entry.effective_connectors()}
        assert statuses == {"Live CRM": "CONNECTED", "Live ERP": "STANDBY"}
        summary = _instance_summary(entry, reg)
        assert {c["name"]: c["status"] for c in summary["connectors"]} == {
            "Live CRM": "CONNECTED",
            "Live ERP": "STANDBY",
        }

        handle = start_workshop_server(reg, host="127.0.0.1", port=0)
        try:
            req = urllib.request.Request(
                f"{handle.base_url}/api/instance/unplug",
                data=json.dumps({"instance": "inst-drivers"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            assert unplug_calls == 1
            assert set(body["unplugged"]) == {"Live CRM", "Live ERP"}
            assert all(c["status"] == "UNPLUGGED" for c in _instance_summary(entry, reg)["connectors"])
            assert all(d["status"] == "UNPLUGGED" for d in bus.list_devices())
        finally:
            handle.stop()
    finally:
        rt.close()


# ---------------------------------------------------------------------------
# 2. python -m open_ontology.design check | verify CLI Red & Green Tests
# ---------------------------------------------------------------------------
def test_design_cli_check_red_cases_fail_closed_with_error_codes(tmp_path: Path) -> None:
    abi_path = tmp_path / "target_abi.json"
    abi_path.write_text(
        json.dumps(build_locked_enterprise_target_blueprint().model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )

    # Red 1: misspelled role ("sales_manger") -> UNKNOWN_ROLE
    bad_role_ov = tmp_path / "ov_bad_role.json"
    ov1 = build_reference_customer_overlay().model_dump(mode="json")
    ov1["add_action_preconditions"]["confirm_o2c_order"][0]["must_hold"] = {
        "op": "eq",
        "left": {"ref": "actor.role"},
        "right": {"lit": "sales_manger"},
    }
    bad_role_ov.write_text(json.dumps(ov1, indent=2), encoding="utf-8")
    rc1, res1 = _run_design_cli(
        [
            "check",
            "--blueprint",
            str(abi_path),
            "--overlay",
            str(bad_role_ov),
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
        ]
    )
    assert rc1 == 1 and res1["ok"] is False
    assert "UNKNOWN_ROLE" in res1["error_codes"]

    # Red 2: destructive overlay trap field -> OVERLAY_BREAKS_STANDARD_CONTRACT
    bad_dest_ov = tmp_path / "ov_destructive.json"
    bad_dest_ov.write_text(
        json.dumps(
            {
                "overlay_id": "destructive_ov",
                "base_model_id": "std_enterprise_crm_o2c",
                "remove_object_types": ["O2cOrder"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    rc2, res2 = _run_design_cli(
        [
            "check",
            "--blueprint",
            str(abi_path),
            "--overlay",
            str(bad_dest_ov),
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
        ]
    )
    assert rc2 == 1 and res2["ok"] is False
    assert "OVERLAY_BREAKS_STANDARD_CONTRACT" in res2["error_codes"]

    # Build temporary mudpit CRM SQLite for Red 3 & Red 4 device binding checks
    dbs = build_mudpit_crm_and_erp_sqlite_pair(tmp_path / "data")

    # Red 3: MISSING_REVERSE_ENUM_TARGET ("Resolved": "RESOLVED" omitted)
    crm_map_no_rev = build_reference_crm_mapping()
    tk_em = next(em for em in crm_map_no_rev.entity_mappings if em.target_object_type == "CrmTicket")
    tk_status_tf = next(ft for ft in tk_em.field_transforms if ft.target_field == "status")
    tk_status_tf.enum_map.pop("Resolved", None)
    map_r3 = tmp_path / "crm_map_r3.json"
    map_r3.write_text(json.dumps(crm_map_no_rev.model_dump(mode="json"), indent=2), encoding="utf-8")
    dev_r3 = tmp_path / "dev_r3.json"
    dev_r3.write_text(
        json.dumps(
            {
                "device_id": "device_crm",
                "db_path": str(dbs["crm_db"]),
                "mapping_path": str(map_r3),
                "handled_actions": ["win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"],
                "owned_object_types": ["CrmAccount", "CrmDeal", "CrmTicket"],
                "shared_object_types": ["BusinessParty"],
            }
        ),
        encoding="utf-8",
    )
    rc3, res3 = _run_design_cli(
        [
            "check",
            "--blueprint",
            str(abi_path),
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
            "--device",
            str(dev_r3),
        ]
    )
    assert rc3 == 1 and res3["ok"] is False
    assert "MISSING_REVERSE_ENUM_TARGET" in res3["error_codes"]

    # Red 4: ACTION_ID_PREFIX_MISMATCH (win_crm_deal id_prefix="WRONG-" != "DEAL-")
    crm_map_bad_prefix = build_reference_crm_mapping()
    win_b = next(b for b in crm_map_bad_prefix.action_bindings if b.action_name == "win_crm_deal")
    win_b.id_prefix = "WRONG-"
    map_r4 = tmp_path / "crm_map_r4.json"
    map_r4.write_text(json.dumps(crm_map_bad_prefix.model_dump(mode="json"), indent=2), encoding="utf-8")
    dev_r4 = tmp_path / "dev_r4.json"
    dev_r4.write_text(
        json.dumps(
            {
                "device_id": "device_crm",
                "db_path": str(dbs["crm_db"]),
                "mapping_path": str(map_r4),
                "handled_actions": ["win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"],
                "owned_object_types": ["CrmAccount", "CrmDeal", "CrmTicket"],
                "shared_object_types": ["BusinessParty"],
            }
        ),
        encoding="utf-8",
    )
    rc4, res4 = _run_design_cli(
        [
            "check",
            "--blueprint",
            str(abi_path),
            "--seed",
            "examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
            "--device",
            str(dev_r4),
        ]
    )
    assert rc4 == 1 and res4["ok"] is False
    assert "ACTION_ID_PREFIX_MISMATCH" in res4["error_codes"]


def test_design_cli_two_device_mudpit_end_to_end_and_referee_audit(tmp_path: Path) -> None:
    bottlecrm_sha_before = _sha256(BOTTLECRM_SOURCE_DB)
    prep = prepare_isolated_player_workspace(tmp_path)
    out_dir = tmp_path / "out"

    (out_dir / "device_crm_mapping.json").write_text(
        json.dumps(build_reference_crm_mapping().model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "device_erp_mapping.json").write_text(
        json.dumps(build_reference_erp_mapping().model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "entity_resolution.json").write_text(
        json.dumps(build_reference_entity_resolution_spec().model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "customer_overlay.json").write_text(
        json.dumps(build_reference_customer_overlay().model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    chk = subprocess.run(["./verify_driver.sh", "check"], cwd=str(tmp_path), text=True, capture_output=True)
    assert chk.returncode == 0, chk.stdout + chk.stderr
    chk_json = json.loads(chk.stdout)
    assert chk_json["ok"] is True

    ver = subprocess.run(["./verify_driver.sh", "verify"], cwd=str(tmp_path), text=True, capture_output=True)
    assert ver.returncode == 0, ver.stdout + ver.stderr
    ver_json = json.loads(ver.stdout)
    assert ver_json["ok"] is True and ver_json["verdict"] == "CORROBORATED"

    audit = _independent_referee_audit(
        player_ws=tmp_path,
        readonly_hashes_before=prep["readonly_hashes"],
        bottlecrm_sha_before=prep["bottlecrm_source_sha256"],
    )
    r1 = audit["round_1_verify"]
    r2 = audit["round_2_verify_after_l4_work_card"]
    assert r1["verdict"] == "CORROBORATED"
    assert r1["falsification"]["action_coverage_ratio"] == 1.0
    assert r1["falsification"]["transition_coverage_ratio"] >= 0.80
    assert any(bp["reason_code"] == "CONFLICTING_HARD_KEY" for bp in r1["resolution_summary"]["blocked_pairs"])
    assert len(r1["resolution_summary"]["ambiguous_candidates"]) >= 1

    assert r2["verdict"] == "CORROBORATED"
    assert r2["resolution_summary"]["ambiguous_candidates"] == []
    assert r2["resolution_summary"]["stats"]["l0_confirmed_entities"] >= 1

    bus_scen = audit["live_coupled_bus_scenarios"]
    assert bus_scen["hq_blocked_initially"] is True
    assert bus_scen["hq_blocked_error_code"] == "OPEN_URGENT_CRM_TICKET_BLOCKS_ORDER"
    assert bus_scen["subsidiary_conflicting_tax_not_blocked_status"] == "COMMITTED"
    assert bus_scen["ticket_resolved_status"] == "COMMITTED"
    assert bus_scen["hq_confirmed_after_ticket_resolved_status"] == "COMMITTED"

    assert _sha256(BOTTLECRM_SOURCE_DB) == bottlecrm_sha_before
