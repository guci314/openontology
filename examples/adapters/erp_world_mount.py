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

from examples.business_ontology import BUSINESS_SEED_ONTOLOGY
from examples.std_enterprise_world_model import (
    build_enterprise_standard_blueprint,
    build_enterprise_standard_spec,
)
from open_ontology.design import _load_thin_driver_factory
from open_ontology.drivers import DeviceDriverRegistry, MountedDevice
from open_ontology.induction import AdapterMappingSpec, CustomerOverlaySpec, DeclarativeBlueprint, compile_blueprint
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


class _CompiledTypeQueryCtx:
    """Resolve base std module ObjectType classes onto the compiled overlay-aware OntologySpec."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def _resolve_cls(self, cls: Any) -> Any:
        if isinstance(cls, type):
            try:
                if not cls.is_abstract():
                    return self._inner.spec.get_object_type(cls)
            except Exception:
                return cls
        return cls

    def get(self, cls: Any, object_id: str) -> Any:
        return self._inner.get(self._resolve_cls(cls), object_id)

    def require(self, cls: Any, object_id: str) -> Any:
        return self._inner.require(self._resolve_cls(cls), object_id)

    def query(self, cls: Any) -> list[Any]:
        return self._inner.query(self._resolve_cls(cls))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _attach_standard_functions(spec: OntologySpec) -> None:
    """Attach standard Python read-only ``@spec.function`` definitions onto compiled ``spec``."""
    py_spec = build_enterprise_standard_spec()
    for fn_name, fn_def in py_spec.functions.items():
        if fn_name in spec.functions:
            continue
        raw_handler = fn_def.handler

        def _wrapped(ctx: Any, params: Any, _fn: Any = raw_handler) -> Any:
            return _fn(_CompiledTypeQueryCtx(ctx), params)

        spec.function(
            fn_name,
            fn_def.params,
            description=fn_def.description,
            view_tags=getattr(fn_def, "view_tags", ()),
        )(_wrapped)


def _mark_hybrid_ownership(
    spec: OntologySpec,
    *,
    standalone_object_types: Sequence[str],
    standalone_actions: Sequence[str],
) -> None:
    """Configure hybrid world ownership: ERP-backed types stay source-backed; missing domains become owned."""
    for ot_name in standalone_object_types:
        if ot_name not in spec.object_types:
            continue
        cls = spec.get_object_type(ot_name)
        cls.__owned__ = True
        prop_map = getattr(cls, "__object_properties__", None)
        if isinstance(prop_map, dict):
            for fname, decl in list(prop_map.items()):
                owned_decl = decl.model_copy(update={"owned": True})
                prop_map[fname] = owned_decl
                if decl.name in spec.object_properties:
                    spec.object_properties[decl.name] = owned_decl

    for act_name in standalone_actions:
        if act_name in spec.actions:
            spec.actions[act_name].affects_source = False


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
    spec = compile_blueprint(blueprint, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    _attach_standard_functions(spec)

    _mark_hybrid_ownership(
        spec,
        standalone_object_types=(*_CORE_OWNED_TYPES, *_CRM_TYPES, *_P2M_TYPES, "TrialBalanceSnapshot", *_H2R_TYPES),
        standalone_actions=(*_WORK_CARD_ACTIONS, *_CRM_ACTIONS, *_P2M_ACTIONS, *_H2R_ALL_ACTIONS, *_ODOO_OVERLAY_ACTIONS),
    )

    _, o2c_bridge, _, _ = load_adapter_package("odoo19", "o2c", ep_copy)
    _, p2p_bridge, _, _ = load_adapter_package("odoo19", "p2p", ep_copy)
    _, r2r_bridge, _, _ = load_adapter_package("odoo19", "r2r", ep_copy)

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
    spec = compile_blueprint(blueprint, seed_ontology=BUSINESS_SEED_ONTOLOGY)
    _attach_standard_functions(spec)

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
