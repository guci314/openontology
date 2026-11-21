"""Pure M2 device driver registry and coupled source-bridge multiplexer.

Maps logical actions and exclusive/shared object types onto concrete mounted
``SourceBridge`` adapters with strict fail-closed routing, atomic hot-swap,
single-action single-device write isolation, and deterministic readback merging.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Mapping

from .settlement import (
    SOURCE_REVERT_FORBIDDEN,
    UNBOUND_SOURCE_BRIDGE,
    SourceBridge,
    call_observe,
)
from .spec import GovernanceViolationError, InvariantError, OntologySpec

__all__ = [
    "DeviceStatus",
    "MountedDevice",
    "DeviceDriverRegistry",
    "CROSS_DEVICE_WRITE_FORBIDDEN",
    "DUPLICATE_DEVICE_ID",
    "DUPLICATE_ACTION_DRIVER",
    "DUPLICATE_OWNED_OBJECT_DRIVER",
]

DeviceStatus = Literal["MOUNTED", "DEGRADED", "UNPLUGGED"]

CROSS_DEVICE_WRITE_FORBIDDEN = "CROSS_DEVICE_WRITE_FORBIDDEN"
DUPLICATE_DEVICE_ID = "DUPLICATE_DEVICE_ID"
DUPLICATE_ACTION_DRIVER = "DUPLICATE_ACTION_DRIVER"
DUPLICATE_OWNED_OBJECT_DRIVER = "DUPLICATE_OWNED_OBJECT_DRIVER"

_VALID_DEVICE_STATUSES = frozenset({"MOUNTED", "DEGRADED", "UNPLUGGED"})


def _normalize_str_tuple(items: Iterable[str]) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in items:
        item = str(raw).strip()
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return tuple(deduped)


def _is_blank(val: Any) -> bool:
    if val is None or val is False:
        return True
    if isinstance(val, str):
        return not val.strip()
    return False


def _resolve_pk_field(
    spec: OntologySpec | None,
    type_name: str,
    rows: list[dict[str, Any]],
) -> str | None:
    if spec is not None and type_name in spec.object_types:
        return spec.get_object_type(type_name).primary_key_name()
    if not rows:
        return None
    sample = rows[0]
    if "id" in sample:
        return "id"
    for key in sample:
        if str(key).endswith("_id"):
            return str(key)
    for key in sample:
        return str(key)
    return None


@dataclass
class MountedDevice:
    """Descriptor for one registered external driver attached to the runtime bus."""

    device_id: str
    name: str
    kind: str
    endpoint: str
    bridge: SourceBridge
    handled_actions: tuple[str, ...] = ()
    owned_object_types: tuple[str, ...] = ()
    shared_object_types: tuple[str, ...] = ()
    status: DeviceStatus = "MOUNTED"
    allow_source_revert: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clean_id = str(self.device_id).strip()
        if not clean_id:
            raise ValueError("device_id must not be empty")
        self.device_id = clean_id
        self.name = str(self.name).strip() or clean_id
        self.kind = str(self.kind).strip() or "external"
        self.endpoint = str(self.endpoint).strip()
        self.handled_actions = _normalize_str_tuple(self.handled_actions)
        self.owned_object_types = _normalize_str_tuple(self.owned_object_types)
        self.shared_object_types = _normalize_str_tuple(self.shared_object_types)
        if self.status not in _VALID_DEVICE_STATUSES:
            raise ValueError(
                f"invalid device status {self.status!r}; expected one of {sorted(_VALID_DEVICE_STATUSES)}"
            )
        overlap = set(self.owned_object_types) & set(self.shared_object_types)
        if overlap:
            raise InvariantError(
                f"device {self.device_id!r} declares {sorted(overlap)} as both owned and shared",
                code=DUPLICATE_OWNED_OBJECT_DRIVER,
            )
        self.metadata = dict(self.metadata)


class DeviceDriverRegistry:
    """Thread-safe pure M2 bus multiplexing multiple external SourceBridge drivers."""

    def __init__(self, *, spec: OntologySpec | None = None) -> None:
        self._spec = spec
        self._lock = threading.RLock()
        self._devices: dict[str, MountedDevice] = {}

    def _validate_active_conflicts(
        self,
        candidate: MountedDevice,
        *,
        exclude_device_id: str | None = None,
    ) -> None:
        if candidate.status == "UNPLUGGED":
            return
        cand_actions = set(candidate.handled_actions)
        cand_owned = set(candidate.owned_object_types)
        cand_shared = set(candidate.shared_object_types)
        for existing_id, existing in self._devices.items():
            if existing_id == exclude_device_id or existing.status == "UNPLUGGED":
                continue
            dup_actions = sorted(cand_actions & set(existing.handled_actions))
            if dup_actions:
                raise InvariantError(
                    f"action(s) {dup_actions} already claimed by active device {existing_id!r}",
                    code=DUPLICATE_ACTION_DRIVER,
                )
            dup_owned = sorted(cand_owned & set(existing.owned_object_types))
            if dup_owned:
                raise InvariantError(
                    f"owned object type(s) {dup_owned} already claimed by active device {existing_id!r}",
                    code=DUPLICATE_OWNED_OBJECT_DRIVER,
                )
            owned_vs_shared = sorted(
                (cand_owned & set(existing.shared_object_types))
                | (cand_shared & set(existing.owned_object_types))
            )
            if owned_vs_shared:
                raise InvariantError(
                    f"object type(s) {owned_vs_shared} cannot mix exclusive owned and shared registration "
                    f"between {candidate.device_id!r} and {existing_id!r}",
                    code=DUPLICATE_OWNED_OBJECT_DRIVER,
                )

    def mount(self, device: MountedDevice) -> MountedDevice:
        """Mount a new external device driver onto the registry bus."""
        with self._lock:
            if device.device_id in self._devices:
                raise InvariantError(
                    f"device_id {device.device_id!r} is already mounted",
                    code=DUPLICATE_DEVICE_ID,
                )
            self._validate_active_conflicts(device)
            self._devices[device.device_id] = copy.copy(device)
            return self._devices[device.device_id]

    def unmount(self, device_id: str, *, remove: bool = False) -> MountedDevice:
        """Mark a device UNPLUGGED (or remove it) so its actions fail closed."""
        with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                raise KeyError(f"unknown device_id {device_id!r}")
            if remove:
                return self._devices.pop(device_id)
            dev.status = "UNPLUGGED"
            return dev

    def set_status(self, device_id: str, status: DeviceStatus) -> MountedDevice:
        """Update device status while preserving active action/ownership uniqueness."""
        if status not in _VALID_DEVICE_STATUSES:
            raise ValueError(f"invalid device status {status!r}")
        with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                raise KeyError(f"unknown device_id {device_id!r}")
            prev_status = dev.status
            probe = copy.copy(dev)
            probe.status = status
            try:
                self._validate_active_conflicts(probe, exclude_device_id=device_id)
                dev.status = status
            except Exception:
                dev.status = prev_status
                raise
            return dev

    def replace(self, device_id: str, new_device: MountedDevice) -> MountedDevice:
        """Atomically hot-swap an existing mounted driver, rolling back on conflict."""
        with self._lock:
            if device_id not in self._devices:
                raise KeyError(f"unknown device_id {device_id!r}")
            snapshot = {k: copy.copy(v) for k, v in self._devices.items()}
            try:
                if new_device.device_id != device_id and new_device.device_id in self._devices:
                    raise InvariantError(
                        f"replacement target device_id {new_device.device_id!r} already exists",
                        code=DUPLICATE_DEVICE_ID,
                    )
                self._validate_active_conflicts(new_device, exclude_device_id=device_id)
                if new_device.device_id != device_id:
                    reordered: dict[str, MountedDevice] = {}
                    for k, v in self._devices.items():
                        if k == device_id:
                            reordered[new_device.device_id] = copy.copy(new_device)
                        else:
                            reordered[k] = v
                    self._devices = reordered
                else:
                    self._devices[device_id] = copy.copy(new_device)
                return self._devices[new_device.device_id]
            except Exception:
                self._devices = snapshot
                raise

    def get_device(self, device_id: str) -> MountedDevice:
        with self._lock:
            dev = self._devices.get(device_id)
            if dev is None:
                raise KeyError(f"unknown device_id {device_id!r}")
            return copy.copy(dev)

    def list_devices(self) -> list[dict[str, Any]]:
        """Return a read-only summary list of all registered devices and statuses."""
        with self._lock:
            return [
                {
                    "device_id": dev.device_id,
                    "name": dev.name,
                    "kind": dev.kind,
                    "endpoint": dev.endpoint,
                    "status": dev.status,
                    "handled_actions": list(dev.handled_actions),
                    "owned_object_types": list(dev.owned_object_types),
                    "shared_object_types": list(dev.shared_object_types),
                    "allow_source_revert": bool(
                        dev.allow_source_revert or dev.bridge.allow_source_revert
                    ),
                    "metadata": copy.deepcopy(dev.metadata),
                }
                for dev in self._devices.values()
            ]

    def _default_merge_shared_rows(
        self,
        type_name: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        pk_field = _resolve_pk_field(self._spec, type_name, rows)
        merged_by_pk: dict[str, dict[str, Any]] = {}
        key_sequence: list[str] = []
        fallback_rows: list[dict[str, Any]] = []
        for row in rows:
            item = copy.deepcopy(row)
            raw_pk = item.get(pk_field) if pk_field else None
            if raw_pk is None or str(raw_pk).strip() == "":
                fallback_rows.append(item)
                continue
            pk = str(raw_pk)
            if pk not in merged_by_pk:
                merged_by_pk[pk] = item
                key_sequence.append(pk)
                continue
            existing = merged_by_pk[pk]
            for fname, val in item.items():
                if (fname not in existing or _is_blank(existing.get(fname))) and not _is_blank(val):
                    existing[fname] = copy.deepcopy(val)
        return [merged_by_pk[pk] for pk in key_sequence] + fallback_rows

    def _observe_impl(
        self,
        move: dict[str, Any] | None = None,
        *,
        shared_mergers: Mapping[str, Callable[[list[dict[str, Any]]], list[dict[str, Any]]]] | None = None,
    ) -> Any:
        with self._lock:
            mounted = [copy.copy(d) for d in self._devices.values() if d.status == "MOUNTED"]
        if not mounted:
            return {}

        requested_types: set[str] = set()
        changes: list[dict[str, Any]] = []
        action_name = ""
        if move is not None:
            action_name = str(move.get("action_type") or "")
            changes = list((move.get("object_diff") or {}).get("object_changes") or [])
            requested_types = {
                str(ch.get("object_type") or "")
                for ch in changes
                if str(ch.get("object_type") or "")
            }

        target_devices: list[MountedDevice] = []
        if move is None:
            target_devices = mounted
        elif requested_types:
            for dev in mounted:
                dev_types = set(dev.owned_object_types) | set(dev.shared_object_types)
                if not dev_types or (dev_types & requested_types):
                    target_devices.append(dev)
        else:
            for dev in mounted:
                if action_name and action_name in dev.handled_actions:
                    target_devices.append(dev)

        objects_by_type: dict[str, list[dict[str, Any]]] = {}
        shared_types_seen: set[str] = set()
        for dev in mounted:
            shared_types_seen.update(dev.shared_object_types)

        seen_type_sources: dict[str, int] = {}
        merged_links: dict[str, list[tuple[str, str]]] = {}
        any_links = False

        for dev in target_devices:
            if move is None:
                sub_move = None
            else:
                dev_types = set(dev.owned_object_types) | set(dev.shared_object_types)
                sub_move = copy.deepcopy(move)
                if dev_types and changes:
                    sub_move.setdefault("object_diff", {})["object_changes"] = [
                        copy.deepcopy(ch)
                        for ch in changes
                        if str(ch.get("object_type") or "") in dev_types
                    ]
            obs_objs, obs_links = call_observe(dev.bridge, sub_move, spec=self._spec)
            for tname, rows in obs_objs.items():
                seen_type_sources[tname] = seen_type_sources.get(tname, 0) + 1
                objects_by_type.setdefault(tname, []).extend(copy.deepcopy(list(rows)))
            if obs_links is not None:
                any_links = True
                for ltype, edges in obs_links.items():
                    bucket = merged_links.setdefault(ltype, [])
                    for edge in edges:
                        pair = (str(edge[0]), str(edge[1]))
                        if pair not in bucket:
                            bucket.append(pair)

        mergers = shared_mergers or {}
        final_objects: dict[str, list[dict[str, Any]]] = {}
        for tname, rows in objects_by_type.items():
            if tname in mergers:
                final_objects[tname] = list(mergers[tname](copy.deepcopy(rows)))
            elif tname in shared_types_seen or seen_type_sources.get(tname, 0) > 1:
                final_objects[tname] = self._default_merge_shared_rows(tname, rows)
            else:
                final_objects[tname] = rows

        if any_links:
            return final_objects, merged_links
        return final_objects

    def observe_all(
        self,
        *,
        shared_mergers: Mapping[str, Callable[[list[dict[str, Any]]], list[dict[str, Any]]]] | None = None,
    ) -> Any:
        """Observe complete snapshots across all currently MOUNTED devices."""
        return self._observe_impl(None, shared_mergers=shared_mergers)

    def as_source_bridge(
        self,
        *,
        shared_mergers: Mapping[str, Callable[[list[dict[str, Any]]], list[dict[str, Any]]]] | None = None,
    ) -> SourceBridge:
        """Project this registry into a single unified ``SourceBridge`` for ``OntologyRuntime``."""

        def _actuate(move: dict[str, Any]) -> Any:
            action_name = str(move.get("action_type") or "")
            is_revert = bool(move.get("is_revert"))
            with self._lock:
                all_devices = [copy.copy(d) for d in self._devices.values()]

            owner_by_type: dict[str, str] = {}
            mounted_claimants: list[MountedDevice] = []
            for dev in all_devices:
                if dev.status != "UNPLUGGED":
                    for ot in dev.owned_object_types:
                        owner_by_type[ot] = dev.device_id
                if dev.status == "MOUNTED" and action_name in dev.handled_actions:
                    mounted_claimants.append(dev)

            if len(mounted_claimants) != 1:
                raise GovernanceViolationError(
                    f"action {action_name!r} has {len(mounted_claimants)} active MOUNTED drivers",
                    code=UNBOUND_SOURCE_BRIDGE,
                )
            target_dev = mounted_claimants[0]
            if is_revert and not (
                target_dev.allow_source_revert or target_dev.bridge.allow_source_revert
            ):
                raise GovernanceViolationError(
                    f"mounted device {target_dev.device_id!r} forbids source revert on {action_name!r}",
                    code=SOURCE_REVERT_FORBIDDEN,
                )

            changes = (move.get("object_diff") or {}).get("object_changes") or []
            touched_owners = {
                owner_by_type[str(ch.get("object_type") or "")]
                for ch in changes
                if str(ch.get("object_type") or "") in owner_by_type
            }
            if len(touched_owners) >= 2 or (
                touched_owners and touched_owners != {target_dev.device_id}
            ):
                raise GovernanceViolationError(
                    f"action {action_name!r} routed to {target_dev.device_id!r} touches exclusive objects "
                    f"owned by {sorted(touched_owners)}; single action may write at most one mounted device",
                    code=CROSS_DEVICE_WRITE_FORBIDDEN,
                )

            return target_dev.bridge.actuate(copy.deepcopy(move))

        def _observe(move: dict[str, Any] | None = None) -> Any:
            return self._observe_impl(move, shared_mergers=shared_mergers)

        with self._lock:
            any_revert = any(
                d.status == "MOUNTED" and (d.allow_source_revert or d.bridge.allow_source_revert)
                for d in self._devices.values()
            )
        return SourceBridge(
            actuate=_actuate,
            observe=_observe,
            allow_source_revert=any_revert,
        )
