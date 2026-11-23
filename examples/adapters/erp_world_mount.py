"""Hybrid enterprise world mounters for Odoo 19 and jshERP blind AI-FDE adapters.

Each mounter compiles the locked six-domain ``std_enterprise_world_model`` blueprint
plus its ERP-specific monotonic ``CustomerOverlaySpec``, mounts the 3 domain
thin drivers onto a single ``DeviceDriverRegistry`` bus, marks the unmounted domains
as ontology-owned (``__owned__ = True`` and ``affects_source = False``) inside the
same ``OntologyRuntime(..., world_mode="coupled")``, and optionally hydrates the
live ERP snapshot into the runtime.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import create_model

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.std_enterprise_world_model import (
    build_enterprise_standard_blueprint,
    build_enterprise_standard_spec,
)
from open_ontology.design import _load_thin_driver_factory
from open_ontology.drivers import DeviceDriverRegistry, MountedDevice
from open_ontology.induction import (
    AdapterMappingSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    apply_overlay_to_spec,
)
from open_ontology.runtime import OntologyRuntime
from open_ontology.settlement import SourceBridge, call_observe
from open_ontology.spec import Actor, OntologySpec

ADAPTER_ROOT = Path(__file__).resolve().parent

_WORK_CARD_ACTIONS: tuple[str, ...] = (
    "open_work_card",
    "claim_work_card",
    "complete_work_card",
    "reject_work_card",
    "mark_work_card_sla_breached",
    "cancel_work_card",
)
_CRM_ACTIONS: tuple[str, ...] = ("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket")
_P2M_ACTIONS: tuple[str, ...] = (
    "release_work_order",
    "issue_material_to_work_order",
    "record_production_output",
    "close_work_order",
)
_R2R_ACTIONS: tuple[str, ...] = (
    "post_journal_entry",
    "reverse_journal_entry",
    "start_fiscal_period_closing",
    "close_fiscal_period",
)
_H2R_ALL_ACTIONS: tuple[str, ...] = (
    "reparent_department",
    "assign_employee_position",
    "grant_authority_mandate",
    "offboard_employee",
)
_CORE_OWNED_TYPES: tuple[str, ...] = ("TradeResource", "TradeCommitment", "FulfillmentEvent", "WorkCard")
_CRM_TYPES: tuple[str, ...] = ("CrmAccount", "CrmDeal", "CrmTicket")
_P2M_TYPES: tuple[str, ...] = ("BomSpec", "WorkOrder", "MaterialConsumption", "ProductionOutput")
_R2R_TYPES: tuple[str, ...] = ("FiscalPeriod", "GlAccount", "JournalEntry", "TrialBalanceSnapshot")
_H2R_TYPES: tuple[str, ...] = ("Department", "JobPosition", "Employee", "AuthorityMandate")

_ODOO_OVERLAY_ACTIONS: tuple[str, ...] = (
    "tag_odoo_order_sales_channel",
    "set_odoo_po_buyer_note",
    "tag_odoo_journal_code",
)
_JSHERP_OVERLAY_ACTIONS: tuple[str, ...] = (
    "tag_jsh_po_settlement_account",
    "tag_jsh_order_depot",
    "annotate_jsh_employee_roster",
    "revoke_authority_mandate",
)


@dataclass
class MountedErpWorld:
    """Bundle returned by :func:`mount_odoo19_enterprise_world` and :func:`mount_jsherp_enterprise_world`."""

    erp: str
    blueprint: DeclarativeBlueprint
    spec: OntologySpec
    registry: DeviceDriverRegistry
    runtime: OntologyRuntime
    overlays: tuple[CustomerOverlaySpec, ...]
    endpoint_config: dict[str, Any]

    def close(self) -> None:
        self.runtime.close()

    def __enter__(self) -> "MountedErpWorld":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def load_customer_overlay(erp: str, overlay_path: Path | str | None = None) -> CustomerOverlaySpec:
    """Load and validate ``examples/adapters/<erp>/overlay.json``."""
    target = Path(overlay_path) if overlay_path is not None else (ADAPTER_ROOT / erp / "overlay.json")
    return CustomerOverlaySpec.model_validate(json.loads(target.read_text("utf-8")))


def load_adapter_package(
    erp: str,
    domain: str,
    endpoint_config: Mapping[str, Any],
) -> tuple[AdapterMappingSpec, SourceBridge, Path, Path]:
    """Load ``mapping.json`` and construct ``SourceBridge`` from ``driver.py`` without modifying generated files."""
    dom_dir = ADAPTER_ROOT / erp / domain
    driver_path = dom_dir / "driver.py"
    mapping_path = dom_dir / "mapping.json"
    mapping = AdapterMappingSpec.model_validate(json.loads(mapping_path.read_text("utf-8")))
    factory = _load_thin_driver_factory(driver_path)
    bridge = factory(copy.deepcopy(dict(endpoint_config)), mapping)
    return mapping, bridge, driver_path, mapping_path


def _mark_hybrid_ownership(
    spec: OntologySpec,
    *,
    standalone_object_types: Sequence[str],
    standalone_actions: Sequence[str],
) -> None:
    """Configure hybrid world ownership without mutating module-level standard classes or ActionDefs."""
    for ot_name in standalone_object_types:
        if ot_name not in spec.object_types:
            continue
        base_cls = spec.get_object_type(ot_name)
        cloned_cls = create_model(
            ot_name,
            __base__=base_cls,
            __module__="open_ontology_hybrid_mount",
        )
        cloned_cls.__primary_key__ = base_cls.primary_key_name()
        cloned_cls.__title__ = getattr(base_cls, "__title__", None)
        cloned_cls.__aggregate_root__ = base_cls.is_aggregate_root()
        cloned_cls.__append_only__ = base_cls.is_append_only()
        cloned_cls.__read_only_source__ = base_cls.is_read_only_source()
        cloned_cls.__owned__ = True
        cloned_cls.__security_tag_fields__ = tuple(
            getattr(base_cls, "__security_tag_fields__", ()) or ()
        )
        cloned_cls.__non_negative_fields__ = tuple(
            getattr(base_cls, "__non_negative_fields__", ()) or ()
        )
        cloned_cls.__masked_fields__ = dict(
            getattr(base_cls, "__masked_fields__", {}) or {}
        )
        cloned_cls.__mask_defaults__ = dict(
            getattr(base_cls, "__mask_defaults__", {}) or {}
        )
        cloned_cls.__interfaces__ = tuple(getattr(base_cls, "__interfaces__", ()) or ())
        cloned_cls.__view_tags__ = tuple(getattr(base_cls, "__view_tags__", ()) or ())
        cloned_cls.__status_field__ = getattr(base_cls, "__status_field__", "status")
        cloned_cls.__initial_statuses__ = getattr(base_cls, "__initial_statuses__", None)
        cloned_cls.__deletable_statuses__ = getattr(base_cls, "__deletable_statuses__", None)
        cloned_cls.__transitions__ = dict(getattr(base_cls, "__transitions__", {}) or {})
        cloned_cls.__voucher_delta_rules__ = dict(
            getattr(base_cls, "__voucher_delta_rules__", {}) or {}
        )
        prop_map = dict(getattr(base_cls, "__object_properties__", None) or {})
        new_prop_map: dict[str, Any] = {}
        for fname, decl in prop_map.items():
            owned_decl = decl.model_copy(update={"owned": True})
            new_prop_map[fname] = owned_decl
            if decl.name in spec.object_properties:
                spec.object_properties[decl.name] = owned_decl
        cloned_cls.__object_properties__ = new_prop_map
        spec.object_types[ot_name] = cloned_cls

    for act_name in standalone_actions:
        if act_name in spec.actions:
            spec.actions[act_name] = spec.actions[act_name].model_copy(
                update={"affects_source": False}
            )


def _toposort_departments(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order ``Department`` rows so parent departments always precede child departments."""
    by_id: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []
    for row in rows:
        did = str(row.get("department_id") or "")
        if did and did not in by_id:
            by_id[did] = row
            ordered_keys.append(did)

    emitted: set[str] = set()
    visiting: set[str] = set()
    out: list[dict[str, Any]] = []

    def _visit(did: str) -> None:
        if did in emitted or did in visiting:
            return
        visiting.add(did)
        row = by_id[did]
        parent = str(row.get("parent_department") or "").strip()
        if parent and parent in by_id and parent != did:
            _visit(parent)
        visiting.remove(did)
        emitted.add(did)
        out.append(row)

    for did in ordered_keys:
        _visit(did)
    return out


def _hydrate_runtime_from_registry(
    runtime: OntologyRuntime,
    registry: DeviceDriverRegistry,
    spec: OntologySpec,
) -> None:
    """Observe M0 across all mounted ERP devices and populate ``runtime`` without triggering automations."""
    obs = registry.observe_all()
    if isinstance(obs, tuple) and len(obs) == 2:
        objects_map = dict(obs[0] or {})
    else:
        objects_map = dict(obs or {})

    seed_actor = Actor(actor_id="system:erp_hydrate", role="admin", clearance_tags={"*"})
    for type_name in spec.object_types:
        raw_rows = list(objects_map.get(type_name) or ())
        if not raw_rows:
            continue
        if type_name == "Department":
            raw_rows = _toposort_departments(raw_rows)
        cls = spec.get_object_type(type_name)
        for row in raw_rows:
            runtime.put_object(cls(**dict(row)), auto_trigger=False, actor=seed_actor)


def _rewrite_entity_id_in_payload(
    raw: Any,
    canonical_entity_id: str,
) -> Any:
    """Normalize local ``AccountingEntity.entity_id`` & ``accounting_entity`` FKs to canonical tenant entity ID."""
    if isinstance(raw, tuple) and len(raw) == 2:
        objs, links = raw
        return _rewrite_entity_id_in_payload(objs, canonical_entity_id), copy.deepcopy(links)
    if not isinstance(raw, Mapping):
        return raw

    copied = copy.deepcopy(dict(raw))
    local_eids: set[str] = set()
    for row in copied.get("AccountingEntity") or ():
        if isinstance(row, dict) and row.get("entity_id"):
            local_eids.add(str(row["entity_id"]))
            row["entity_id"] = canonical_entity_id

    if not local_eids or local_eids == {canonical_entity_id}:
        return copied

    for type_name, rows in copied.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            fk_val = row.get("accounting_entity")
            if fk_val is not None and str(fk_val) in local_eids:
                row["accounting_entity"] = canonical_entity_id
            if type_name == "AccountingEntity" and row.get("entity_id"):
                row["entity_id"] = canonical_entity_id
    return copied


def _build_canonical_entity_bridges(
    *,
    p2p_bridge: SourceBridge,
    o2c_bridge: SourceBridge,
    h2r_bridge: SourceBridge,
    spec: OntologySpec,
) -> tuple[SourceBridge, SourceBridge, SourceBridge]:
    """Align jshERP ``AccountingEntity`` IDs across drivers in-memory without editing driver code.

    ``jsherp/p2p`` and ``jsherp/h2r`` key ``AccountingEntity`` by ``tenantId``
    (``ENT-<tenantId>``), whereas ``jsherp/o2c`` keys ``AccountingEntity`` by
    ``jsh_system_config.id`` (``ENT-<sys_id>``). Wrapping ``observe`` keeps
    the blind-generated drivers immutable while unifying ``AccountingEntity`` cleanly.
    """
    state: dict[str, str | None] = {"canonical_entity_id": None}

    def _record_from_obs(raw: Any) -> Any:
        if state["canonical_entity_id"]:
            return raw
        objs, _ = (raw if isinstance(raw, tuple) and len(raw) == 2 else (raw, None))
        if isinstance(objs, Mapping):
            ents = objs.get("AccountingEntity") or ()
            for row in ents:
                if isinstance(row, Mapping) and row.get("entity_id"):
                    state["canonical_entity_id"] = str(row["entity_id"])
                    break
        return raw

    def _p2p_observe(move: dict[str, Any] | None = None) -> Any:
        objs, links = call_observe(p2p_bridge, move, spec=spec)
        raw: Any = (objs, links) if links is not None else objs
        return _record_from_obs(raw)

    def _h2r_observe(move: dict[str, Any] | None = None) -> Any:
        objs, links = call_observe(h2r_bridge, move, spec=spec)
        raw: Any = (objs, links) if links is not None else objs
        return _record_from_obs(raw)

    def _o2c_observe(move: dict[str, Any] | None = None) -> Any:
        if not state["canonical_entity_id"]:
            _p2p_observe(None)
        if not state["canonical_entity_id"]:
            _h2r_observe(None)
        objs, links = call_observe(o2c_bridge, move, spec=spec)
        raw: Any = (objs, links) if links is not None else objs
        canon = state["canonical_entity_id"]
        if canon:
            return _rewrite_entity_id_in_payload(raw, canon)
        return raw

    return (
        SourceBridge(
            actuate=p2p_bridge.actuate,
            observe=_p2p_observe,
            allow_source_revert=p2p_bridge.allow_source_revert,
        ),
        SourceBridge(
            actuate=o2c_bridge.actuate,
            observe=_o2c_observe,
            allow_source_revert=o2c_bridge.allow_source_revert,
        ),
        SourceBridge(
            actuate=h2r_bridge.actuate,
            observe=_h2r_observe,
            allow_source_revert=h2r_bridge.allow_source_revert,
        ),
    )


def _wrap_odoo_r2r_bridge(
    r2r_bridge: SourceBridge,
    *,
    spec: OntologySpec,
) -> SourceBridge:
    """Correlate Odoo server-assigned ``account.move.reversal`` IDs with M1's predicted ``reversal_of`` ID."""
    rev_aliases: dict[str, dict[str, Any]] = {}
    claimed_raw_ids: set[str] = set()

    def _r2r_observe(move: dict[str, Any] | None = None) -> Any:
        objs, links = call_observe(r2r_bridge, move, spec=spec)
        je_rows = list(objs.get("JournalEntry") or ())
        if isinstance(move, Mapping) and move.get("action_type") == "reverse_journal_entry":
            changes = (move.get("object_diff") or {}).get("object_changes") or ()
            for ch in changes:
                if ch.get("kind") != "create" or ch.get("object_type") != "JournalEntry":
                    continue
                pred_id = str(ch.get("object_id") or "")
                after = dict(ch.get("after") or {})
                orig_id = str(after.get("reversal_of") or "")
                if not pred_id or not orig_id:
                    continue
                for row in je_rows:
                    raw_id = str(row.get("entry_id") or "")
                    if not raw_id or raw_id in claimed_raw_ids:
                        continue
                    if str(row.get("reversal_of") or "") == orig_id:
                        claimed_raw_ids.add(raw_id)
                        rev_aliases[raw_id] = {
                            "entry_id": pred_id,
                            "memo": str(after.get("memo") or ""),
                            "source_event": after.get("source_event"),
                        }
                        break

        if rev_aliases:
            for row in je_rows:
                raw_id = str(row.get("entry_id") or "")
                alias = rev_aliases.get(raw_id)
                if alias is not None:
                    row["entry_id"] = alias["entry_id"]
                    row["memo"] = alias["memo"]
                    if alias.get("source_event") is not None:
                        row["source_event"] = alias["source_event"]
                rev_of = str(row.get("reversal_of") or "")
                if rev_of in rev_aliases:
                    row["reversal_of"] = rev_aliases[rev_of]["entry_id"]
            if links is not None:
                id_map = {k: str(v["entry_id"]) for k, v in rev_aliases.items()}
                rewritten_links: dict[str, list[tuple[str, str]]] = {}
                for ltype, edges in links.items():
                    rewritten_links[ltype] = [
                        (id_map.get(str(s), str(s)), id_map.get(str(t), str(t)))
                        for s, t in edges
                    ]
                links = rewritten_links
        return (objs, links) if links is not None else objs

    return SourceBridge(
        actuate=r2r_bridge.actuate,
        observe=_r2r_observe,
        allow_source_revert=r2r_bridge.allow_source_revert,
    )


def mount_odoo19_enterprise_world(
    endpoint_config: Mapping[str, Any],
    *,
    db_path: str = ":memory:",
    include_overlay: bool = True,
    overlay_path: Path | str | None = None,
    hydrate: bool = True,
) -> MountedErpWorld:
    """Mount Odoo 19 coupled domains (O2C, P2P, R2R) alongside standalone CRM/P2M/H2R."""
    ep_copy = copy.deepcopy(dict(endpoint_config))
    overlays: tuple[CustomerOverlaySpec, ...] = (
        (load_customer_overlay("odoo19", overlay_path),) if (include_overlay or overlay_path is not None) else ()
    )
    blueprint = build_enterprise_standard_blueprint(overlays=overlays)
    spec = build_enterprise_standard_spec()
    for ov in overlays:
        spec = apply_overlay_to_spec(spec, ov, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    _mark_hybrid_ownership(
        spec,
        standalone_object_types=(*_CORE_OWNED_TYPES, *_CRM_TYPES, *_P2M_TYPES, "TrialBalanceSnapshot", *_H2R_TYPES),
        standalone_actions=(*_WORK_CARD_ACTIONS, *_CRM_ACTIONS, *_P2M_ACTIONS, *_H2R_ALL_ACTIONS, *_ODOO_OVERLAY_ACTIONS),
    )

    _, o2c_bridge, _, _ = load_adapter_package("odoo19", "o2c", ep_copy)
    _, p2p_bridge, _, _ = load_adapter_package("odoo19", "p2p", ep_copy)
    _, raw_r2r_bridge, _, _ = load_adapter_package("odoo19", "r2r", ep_copy)
    r2r_bridge = _wrap_odoo_r2r_bridge(raw_r2r_bridge, spec=spec)

    ep_label = str(ep_copy.get("url") or "http://localhost:8069/jsonrpc")
    registry = DeviceDriverRegistry(spec=spec)
    registry.mount(
        MountedDevice(
            device_id="odoo19_o2c",
            name="Odoo 19 O2C Adapter",
            kind="odoo_jsonrpc",
            endpoint=ep_label,
            bridge=o2c_bridge,
            handled_actions=("confirm_o2c_order", "complete_o2c_delivery", "post_o2c_invoice", "record_o2c_payment"),
            owned_object_types=("O2cCustomerProfile", "O2cOrder", "O2cDelivery", "O2cInvoice"),
            shared_object_types=("AccountingEntity", "BusinessParty"),
        )
    )
    registry.mount(
        MountedDevice(
            device_id="odoo19_p2p",
            name="Odoo 19 P2P Adapter",
            kind="odoo_jsonrpc",
            endpoint=ep_label,
            bridge=p2p_bridge,
            handled_actions=("confirm_p2p_order", "record_p2p_receipt", "post_p2p_bill", "record_p2p_payment"),
            owned_object_types=("P2pSupplierProfile", "P2pPurchaseOrder", "P2pGoodsReceipt", "P2pVendorBill"),
            shared_object_types=("AccountingEntity", "BusinessParty"),
        )
    )
    registry.mount(
        MountedDevice(
            device_id="odoo19_r2r",
            name="Odoo 19 R2R Adapter",
            kind="odoo_jsonrpc",
            endpoint=ep_label,
            bridge=r2r_bridge,
            handled_actions=("post_journal_entry", "reverse_journal_entry"),
            owned_object_types=("FiscalPeriod", "GlAccount", "JournalEntry"),
            shared_object_types=("AccountingEntity",),
        )
    )

    runtime = OntologyRuntime(
        spec,
        db_path=db_path,
        world_mode="coupled",
        source_bridge=registry.as_source_bridge(),
    )
    try:
        if hydrate:
            _hydrate_runtime_from_registry(runtime, registry, spec)
    except Exception:
        runtime.close()
        raise

    return MountedErpWorld(
        erp="odoo19",
        blueprint=blueprint,
        spec=spec,
        registry=registry,
        runtime=runtime,
        overlays=overlays,
        endpoint_config=ep_copy,
    )


def mount_jsherp_enterprise_world(
    endpoint_config: Mapping[str, Any],
    *,
    db_path: str = ":memory:",
    include_overlay: bool = True,
    overlay_path: Path | str | None = None,
    hydrate: bool = True,
) -> MountedErpWorld:
    """Mount jshERP coupled domains (P2P, O2C, H2R) alongside standalone CRM/P2M/R2R + AuthorityMandate."""
    ep_copy = copy.deepcopy(dict(endpoint_config))
    overlays: tuple[CustomerOverlaySpec, ...] = (
        (load_customer_overlay("jsherp", overlay_path),) if (include_overlay or overlay_path is not None) else ()
    )
    blueprint = build_enterprise_standard_blueprint(overlays=overlays)
    spec = build_enterprise_standard_spec()
    for ov in overlays:
        spec = apply_overlay_to_spec(spec, ov, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    _mark_hybrid_ownership(
        spec,
        standalone_object_types=(*_CORE_OWNED_TYPES, *_CRM_TYPES, *_P2M_TYPES, *_R2R_TYPES, "AuthorityMandate"),
        standalone_actions=(
            *_WORK_CARD_ACTIONS,
            *_CRM_ACTIONS,
            *_P2M_ACTIONS,
            *_R2R_ACTIONS,
            "grant_authority_mandate",
            *_JSHERP_OVERLAY_ACTIONS,
        ),
    )

    _, p2p_raw, _, _ = load_adapter_package("jsherp", "p2p", ep_copy)
    _, o2c_raw, _, _ = load_adapter_package("jsherp", "o2c", ep_copy)
    _, h2r_raw, _, _ = load_adapter_package("jsherp", "h2r", ep_copy)
    p2p_bridge, o2c_bridge, h2r_bridge = _build_canonical_entity_bridges(
        p2p_bridge=p2p_raw,
        o2c_bridge=o2c_raw,
        h2r_bridge=h2r_raw,
        spec=spec,
    )

    ep_label = str(ep_copy.get("base_url") or ep_copy.get("url") or "http://127.0.0.1:19999/jshERP-boot")
    registry = DeviceDriverRegistry(spec=spec)
    registry.mount(
        MountedDevice(
            device_id="jsherp_p2p",
            name="jshERP P2P Adapter",
            kind="http_rest",
            endpoint=ep_label,
            bridge=p2p_bridge,
            handled_actions=("confirm_p2p_order", "record_p2p_receipt", "post_p2p_bill", "record_p2p_payment"),
            owned_object_types=("P2pSupplierProfile", "P2pPurchaseOrder", "P2pGoodsReceipt", "P2pVendorBill"),
            shared_object_types=("AccountingEntity", "BusinessParty"),
        )
    )
    registry.mount(
        MountedDevice(
            device_id="jsherp_o2c",
            name="jshERP O2C Adapter",
            kind="http_rest",
            endpoint=ep_label,
            bridge=o2c_bridge,
            handled_actions=("confirm_o2c_order", "complete_o2c_delivery", "post_o2c_invoice", "record_o2c_payment"),
            owned_object_types=("O2cCustomerProfile", "O2cOrder", "O2cDelivery", "O2cInvoice"),
            shared_object_types=("AccountingEntity", "BusinessParty"),
        )
    )
    registry.mount(
        MountedDevice(
            device_id="jsherp_h2r",
            name="jshERP H2R Adapter",
            kind="http_rest",
            endpoint=ep_label,
            bridge=h2r_bridge,
            handled_actions=("reparent_department", "assign_employee_position", "offboard_employee"),
            owned_object_types=("Department", "JobPosition", "Employee"),
            shared_object_types=("AccountingEntity",),
        )
    )

    runtime = OntologyRuntime(
        spec,
        db_path=db_path,
        world_mode="coupled",
        source_bridge=registry.as_source_bridge(),
    )
    try:
        if hydrate:
            _hydrate_runtime_from_registry(runtime, registry, spec)
    except Exception:
        runtime.close()
        raise

    return MountedErpWorld(
        erp="jsherp",
        blueprint=blueprint,
        spec=spec,
        registry=registry,
        runtime=runtime,
        overlays=overlays,
        endpoint_config=ep_copy,
    )
