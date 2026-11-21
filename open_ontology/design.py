"""Pure M2 Design-Time compiler & Popperian verification CLI (domain-agnostic).

Provides standalone ``check`` (``javac``-style static composition, monotonic
overlay, closed-role, schema-ref, alignment, and driver-bus checks) and
``verify`` (``JUnit``-style isolated sandbox crucible + multi-device differential
falsification) entry points for external engineering agents and CI harnesses.
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .drivers import DeviceDriverRegistry, MountedDevice
from .induction import (
    AdapterMappingSpec,
    CrucibleContext,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    DifferentialFalsifier,
    EntityMappingSpec,
    EntityResolutionDecision,
    EntityResolutionSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldTransformSpec,
    SeedOntology,
    SourceEntityBatch,
    SymbolicCrucible,
    apply_overlay,
    apply_resolution_decisions,
    blueprint_fingerprint,
    compile_blueprint,
    compose_blueprints,
    inject_crosswalk_into_mapping,
    merge_projected_phenomena,
    project_external_phenomenon,
    project_single_record,
    resolve_entities,
    toposort_entity_mappings,
)
from .induction.decompiler import _validate_decompiled_alignment
from .induction.phenomenon import _apply_field_transform
from .settlement import DEVICE_STATE_MISMATCH, DeviceStateMismatchError, SourceBridge, call_observe
from .spec import Actor, InvariantError, OntologySpec, OpenOntologyError

__all__ = [
    "DesignDeviceSpec",
    "scout_readonly_sqlite_profile",
    "DeclarativeSQLiteDriverBridge",
    "audit_thin_driver_ast",
    "verify_thin_driver_on_hidden_world",
    "run_design_check",
    "run_design_verify",
    "main",
]

_CHECK_IN_RE = re.compile(
    r'CHECK\s*\(\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s+IN\s*\(([^)]+)\)\s*\)',
    re.IGNORECASE,
)
_CATEGORICAL_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ \-]{0,30}$")
_SKIP_FRAMEWORK_PREFIXES = ("sqlite_", "django_", "auth_")

_ALLOWED_THIN_DRIVER_MODULES: frozenset[str] = frozenset(
    {
        "__future__",
        "abc",
        "base64",
        "collections",
        "collections.abc",
        "contextlib",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "hashlib",
        "hmac",
        "http.client",
        "itertools",
        "json",
        "logging",
        "math",
        "numbers",
        "operator",
        "re",
        "sqlite3",
        "ssl",
        "string",
        "time",
        "types",
        "typing",
        "urllib.error",
        "urllib.parse",
        "urllib.request",
        "uuid",
        "xmlrpc.client",
        "open_ontology.settlement",
        "open_ontology.spec",
        "open_ontology.drivers",
        "open_ontology.induction.phenomenon",
        "open_ontology.induction.dsl",
    }
)
_ALLOWED_THIN_DRIVER_ROOT_EXPORTS: dict[str, frozenset[str]] = {
    "urllib": frozenset({"error", "parse", "request"}),
    "http": frozenset({"client"}),
    "xmlrpc": frozenset({"client"}),
    "open_ontology": frozenset(
        {
            "SourceBridge",
            "DeviceStateMismatchError",
            "GovernanceViolationError",
            "InvariantError",
            "settlement",
            "spec",
            "drivers",
        }
    ),
    "open_ontology.induction": frozenset(
        {
            "ActionExecutionBinding",
            "AdapterMappingSpec",
            "EntityMappingSpec",
            "ExternalEntityProfile",
            "ExternalFieldProfile",
            "ExternalPhenomenonProfile",
            "FieldTransformSpec",
            "project_external_phenomenon",
            "project_single_record",
            "toposort_entity_mappings",
        }
    ),
}
_FORBIDDEN_DYN_CALL_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "breakpoint"}
)
_FORBIDDEN_DYN_ATTRS = frozenset(
    {"system", "popen", "__import__", "import_module"}
)
_FORBIDDEN_GETATTR_TARGETS = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "system",
        "popen",
        "open",
        "write_text",
        "write_bytes",
        "unlink",
        "import_module",
        "__builtins__",
        "__subclasses__",
        "__globals__",
    }
)
_FORBIDDEN_FS_WRITE_METHODS = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "chmod",
        "mkdir",
        "makedirs",
        "rmdir",
        "rename",
        "replace",
        "touch",
        "symlink_to",
        "hardlink_to",
        "remove",
        "rmtree",
        "copyfile",
        "copy2",
        "move",
    }
)


class DesignDeviceSpec(BaseModel):
    """Declarative CLI descriptor for one external physical device under test."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    name: str = ""
    kind: str = "sqlite"
    endpoint: str = ""
    db_path: str = ""
    mapping_path: str
    profile_path: str | None = None
    driver_module_path: str | None = None
    endpoint_config: dict[str, Any] = Field(default_factory=dict)
    endpoint_config_path: str | None = None
    forbidden_literals: list[str] = Field(default_factory=list)
    max_driver_lines: int = 350
    reset_hook: str | None = None
    owned_object_types: list[str] = Field(default_factory=list)
    shared_object_types: list[str] = Field(default_factory=list)
    handled_actions: list[str] = Field(default_factory=list)
    allow_source_revert: bool = False


def _extract_error_code(msg: str, default: str = "DESIGN_ERROR") -> str:
    head = str(msg).split(":", 1)[0].strip()
    if head.startswith("[") and "]" in head:
        head = head[1 : head.index("]")].strip()
    if head and re.fullmatch(r"[A-Z][A-Z0-9_]+", head):
        return head
    return default


def _add_error(
    errors: list[str],
    error_codes: list[str],
    msg: str,
    code: str | None = None,
) -> None:
    resolved_code = code or _extract_error_code(msg)
    if msg not in errors:
        errors.append(msg)
    if resolved_code and resolved_code not in error_codes:
        error_codes.append(resolved_code)


def _resolve_existing_path(raw: str, *, base_dir: Path | None = None) -> Path:
    p = Path(raw)
    if p.exists() or p.is_absolute():
        return p.resolve()
    if base_dir is not None:
        candidate = (base_dir / p).resolve()
        if candidate.exists():
            return candidate
    return p.resolve()


def _load_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_blueprint_arg(arg: str) -> DeclarativeBlueprint:
    p = Path(arg)
    if p.is_file() or arg.endswith(".json"):
        if not p.is_file():
            raise FileNotFoundError(f"blueprint file not found: {arg}")
        raw = _load_json_file(p)
        return DeclarativeBlueprint.model_validate(raw)
    if ":" not in arg:
        raise ValueError(
            f"blueprint argument {arg!r} must be a .json path or 'module.path:symbol'"
        )
    mod_name, attr_name = arg.split(":", 1)
    mod = importlib.import_module(mod_name)
    target = getattr(mod, attr_name)
    loaded = target() if callable(target) else target
    if isinstance(loaded, DeclarativeBlueprint):
        return loaded
    return DeclarativeBlueprint.model_validate(loaded)


def _load_seed_arg(arg: str | None) -> SeedOntology | None:
    if not arg:
        return None
    if ":" not in arg:
        raise ValueError(f"seed argument {arg!r} must be 'module.path:ATTR'")
    mod_name, attr_name = arg.split(":", 1)
    mod = importlib.import_module(mod_name)
    target = getattr(mod, attr_name)
    loaded = target() if callable(target) else target
    if not isinstance(loaded, SeedOntology):
        raise ValueError(f"seed {arg!r} did not resolve to SeedOntology")
    return loaded


def _load_device_arg(arg: str) -> tuple[DesignDeviceSpec, Path | None]:
    stripped = arg.strip()
    if stripped.startswith("{"):
        return DesignDeviceSpec.model_validate(json.loads(stripped)), None
    p = Path(arg)
    if len(arg) < 1024 and p.is_file():
        return DesignDeviceSpec.model_validate(_load_json_file(p)), p.resolve().parent
    raise FileNotFoundError(f"device config file not found: {arg}")


def _sanitize_sql_value(val: Any) -> Any:
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val).decode("utf-8", errors="replace")
    return val


def scout_readonly_sqlite_profile(
    db_path: str | Path,
    *,
    source_kind: str = "sqlite",
    include_tables: Sequence[str] = (),
    max_samples_per_table: int = 200,
) -> ExternalPhenomenonProfile:
    """Inspect a SQLite file strictly read-only via URI ``mode=ro``."""
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SQLite file not found: {resolved}")
    requested_tables = set(include_tables)
    conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        entities: list[ExternalEntityProfile] = []
        for trow in table_rows:
            tbl = str(trow["name"])
            ddl = str(trow["sql"] or "")
            is_framework = tbl.startswith(_SKIP_FRAMEWORK_PREFIXES)
            if is_framework and tbl not in requested_tables:
                continue
            row_count = int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0])
            if row_count == 0 and tbl not in requested_tables:
                continue

            cols = conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()
            fks = conn.execute(f'PRAGMA foreign_key_list("{tbl}")').fetchall()
            fk_by_col = {str(fk["from"]): str(fk["table"]) for fk in fks}

            check_enums: dict[str, list[str]] = {}
            for col_match, vals_blob in _CHECK_IN_RE.findall(ddl):
                lits = [v.strip().strip("'\"") for v in vals_blob.split(",") if v.strip()]
                if lits:
                    check_enums[col_match] = lits

            limit_clause = f" LIMIT {int(max_samples_per_table)}" if max_samples_per_table > 0 else ""
            raw_rows = [
                {k: _sanitize_sql_value(r[k]) for k in r.keys()}
                for r in conn.execute(f'SELECT * FROM "{tbl}" ORDER BY rowid{limit_clause}').fetchall()
            ]

            fields: list[ExternalFieldProfile] = []
            for c in cols:
                cname = str(c["name"])
                ctype = str(c["type"] or "varchar").lower()
                is_pk = int(c["pk"] or 0) > 0
                if "int" in ctype or "bool" in ctype:
                    rtype = "integer"
                elif "real" in ctype or "dec" in ctype or "float" in ctype or "num" in ctype:
                    rtype = "float"
                else:
                    rtype = "string"

                enums: list[str] = list(check_enums.get(cname, []))
                if rtype == "string" and not is_pk and cname not in fk_by_col:
                    vals = [
                        str(r[cname])
                        for r in raw_rows
                        if r.get(cname) is not None and str(r.get(cname)).strip() != ""
                    ]
                    distinct = sorted(set(vals))
                    if (
                        1 <= len(distinct) <= 15
                        and all(_CATEGORICAL_TOKEN_RE.match(v) for v in distinct)
                        and (len(distinct) < len(raw_rows) or all(" " not in v for v in distinct))
                    ):
                        enums = sorted(set(enums) | set(distinct))

                fields.append(
                    ExternalFieldProfile(
                        name=cname,
                        raw_type=rtype,
                        label="PRIMARY_KEY" if is_pk else cname,
                        relation_target=fk_by_col.get(cname),
                        enum_values=enums,
                    )
                )
            entities.append(
                ExternalEntityProfile(
                    table_name=tbl,
                    row_count=row_count,
                    fields=fields,
                    sample_rows=raw_rows,
                )
            )
        return ExternalPhenomenonProfile(
            source_kind=source_kind,
            source_locator=f"sqlite://{resolved.name}",
            entities=entities,
        )
    finally:
        conn.close()


class DeclarativeSQLiteDriverBridge:
    """Generic M2 SQLite SourceBridge driven solely by ``AdapterMappingSpec`` + ``PRAGMA``."""

    def __init__(
        self,
        db_path: Path,
        *,
        spec: OntologySpec,
        profile: ExternalPhenomenonProfile,
        adapter_mapping: AdapterMappingSpec,
    ) -> None:
        self.db_path = Path(db_path)
        self.spec = spec
        self.profile = profile
        self.adapter_mapping = adapter_mapping
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.write_count = 0

        ordered_ems, _ = toposort_entity_mappings(list(adapter_mapping.entity_mappings))
        self._ordered_ems: list[EntityMappingSpec] = ordered_ems
        self._ems_by_type: dict[str, list[EntityMappingSpec]] = {}
        for em in ordered_ems:
            self._ems_by_type.setdefault(em.target_object_type, []).append(em)

        self._col_meta: dict[str, dict[str, dict[str, Any]]] = {}
        self._pk_col: dict[str, str] = {}
        for em in ordered_ems:
            tbl = em.source_table
            if tbl in self._col_meta:
                continue
            cols = self.conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()
            self._col_meta[tbl] = {
                str(r["name"]): {"type": str(r["type"] or "").upper(), "pk": int(r["pk"] or 0)}
                for r in cols
            }
            pk_cols = [str(r["name"]) for r in cols if int(r["pk"] or 0) > 0]
            self._pk_col[tbl] = pk_cols[0] if pk_cols else (em.id_transform.source_field or "id")

        self._lookup_index = self._build_lookup_index()

    def close(self) -> None:
        try:
            if self.conn.in_transaction:
                self.conn.rollback()
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass

    def _build_lookup_index(self) -> dict[tuple[str, str, str], str]:
        lookup: dict[tuple[str, str, str], str] = {}
        rows_cache: dict[str, list[dict[str, Any]]] = {}
        for em in self._ordered_ems:
            tbl = em.source_table
            if tbl not in rows_cache:
                try:
                    raw = self.conn.execute(f'SELECT * FROM "{tbl}" ORDER BY rowid').fetchall()
                    rows_cache[tbl] = [{k: _sanitize_sql_value(r[k]) for k in r.keys()} for r in raw]
                except sqlite3.Error:
                    ent = next((e for e in self.profile.entities if e.table_name == tbl), None)
                    rows_cache[tbl] = list(ent.sample_rows) if ent is not None else []

            em_false = (
                em.treat_false_as_null
                if em.treat_false_as_null is not None
                else self.adapter_mapping.treat_false_as_null
            )
            tf_false = (
                em.id_transform.treat_false_as_null
                if em.id_transform.treat_false_as_null is not None
                else em_false
            )
            for row in rows_cache[tbl]:
                cid = _apply_field_transform(
                    row,
                    em.id_transform,
                    lookup,
                    treat_false_as_null=tf_false,
                )
                if cid is None:
                    continue
                cid_str = str(cid)
                for k, v in row.items():
                    if v is not None and not isinstance(v, (list, dict)):
                        lookup[(f"{em.target_object_type}@{tbl}", str(k), str(v))] = cid_str
                        lookup.setdefault((tbl, str(k), str(v)), cid_str)
        return lookup

    def _raw_pk(self, em: EntityMappingSpec, canonical_id: str) -> Any:
        inv_id: dict[str, str] = {}
        for src_k, dst_v in (em.id_transform.enum_map or {}).items():
            inv_id.setdefault(str(dst_v), str(src_k))
        if canonical_id in inv_id:
            raw = inv_id[canonical_id]
        else:
            prefix = em.id_transform.prefix or ""
            raw = canonical_id[len(prefix) :] if prefix and canonical_id.startswith(prefix) else canonical_id
        pk_col = self._pk_col.get(em.source_table, em.id_transform.source_field or "id")
        pk_type = self._col_meta.get(em.source_table, {}).get(pk_col, {}).get("type", "")
        if "INT" in pk_type and raw.lstrip("-").isdigit():
            return int(raw)
        return raw

    def _encode_field_value(
        self,
        ft: FieldTransformSpec,
        value: Any,
        *,
        col_type: str = "",
    ) -> Any:
        if ft.kind == "enum_map":
            inv: dict[str, str] = {}
            for src_k, dst_v in (ft.enum_map or {}).items():
                inv.setdefault(str(dst_v), str(src_k))
            encoded = inv.get(str(value), value)
        elif ft.kind in ("id_prefix", "foreign_key_id"):
            inv_fk: dict[str, str] = {}
            for src_k, dst_v in (ft.enum_map or {}).items():
                inv_fk.setdefault(str(dst_v), str(src_k))
            sval = str(value)
            if sval in inv_fk:
                encoded = inv_fk[sval]
            elif ft.prefix and sval.startswith(ft.prefix):
                encoded = sval[len(ft.prefix) :]
            else:
                encoded = value
        elif ft.kind == "scale_int" and ft.scale:
            encoded = float(value) / ft.scale if value is not None else value
        elif isinstance(value, bool):
            encoded = int(value)
        else:
            encoded = value

        if "INT" in col_type and isinstance(encoded, str) and encoded.lstrip("-").isdigit():
            return int(encoded)
        if "INT" in col_type and isinstance(encoded, float):
            return int(round(encoded))
        return encoded

    def actuate(self, move_envelope: dict[str, Any]) -> None:
        diff_changes = (move_envelope.get("object_diff") or {}).get("object_changes") or []
        before_changes = self.conn.total_changes
        try:
            for ch in diff_changes:
                tname = str(ch.get("object_type") or "")
                oid = str(ch.get("object_id") or "")
                cls = self.spec.get_object_type(tname)
                if cls.is_fully_owned():
                    continue
                ems = self._ems_by_type.get(tname) or []
                if not ems:
                    continue
                em = ems[0]
                pk_col = self._pk_col[em.source_table]
                raw_pk = self._raw_pk(em, oid)
                if len(ems) > 1:
                    for cand_em in ems:
                        cand_pk_col = self._pk_col[cand_em.source_table]
                        cand_raw = self._raw_pk(cand_em, oid)
                        exists = self.conn.execute(
                            f'SELECT 1 FROM "{cand_em.source_table}" WHERE "{cand_pk_col}"=?',
                            (cand_raw,),
                        ).fetchone()
                        if exists is not None:
                            em = cand_em
                            pk_col = cand_pk_col
                            raw_pk = cand_raw
                            break

                owned_fields = set(cls.owned_field_defaults())
                pk_field = cls.primary_key_name()
                before = dict(ch.get("before") or {})
                after = dict(ch.get("after") or {})
                valid_cols = self._col_meta.get(em.source_table, {})
                ft_by_target = {ft.target_field: ft for ft in em.field_transforms}
                updates: dict[str, Any] = {}
                for tf_name, after_val in after.items():
                    if tf_name == pk_field or tf_name in owned_fields or after_val == before.get(tf_name):
                        continue
                    ft = ft_by_target.get(tf_name)
                    if ft is None or ft.kind == "const" or not ft.source_field or ft.source_field not in valid_cols:
                        continue
                    col_type = str(valid_cols[ft.source_field].get("type") or "")
                    updates[ft.source_field] = self._encode_field_value(
                        ft, after_val, col_type=col_type
                    )
                if updates:
                    set_sql = ", ".join(f'"{col}"=?' for col in updates)
                    self.conn.execute(
                        f'UPDATE "{em.source_table}" SET {set_sql} WHERE "{pk_col}"=?',
                        (*updates.values(), raw_pk),
                    )
            self.conn.commit()
            delta = self.conn.total_changes - before_changes
            if delta > 0:
                self.write_count += delta
        except Exception:
            try:
                if self.conn.in_transaction:
                    self.conn.rollback()
            except Exception:
                pass
            raise

    def observe(self, move_envelope: dict[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        self._lookup_index = self._build_lookup_index()
        diff_changes = (
            ((move_envelope or {}).get("object_diff") or {}).get("object_changes") or []
            if move_envelope is not None
            else []
        )
        observed: dict[str, list[dict[str, Any]]] = {}
        if not diff_changes:
            for em in self._ordered_ems:
                tname = em.target_object_type
                rows = self.conn.execute(f'SELECT * FROM "{em.source_table}" ORDER BY rowid').fetchall()
                em_false = (
                    em.treat_false_as_null
                    if em.treat_false_as_null is not None
                    else self.adapter_mapping.treat_false_as_null
                )
                for row in rows:
                    row_dict = {k: _sanitize_sql_value(row[k]) for k in row.keys()}
                    if any(not _row_matches_filter_compat(row_dict, flt) for flt in em.row_filters):
                        continue
                    proj = project_single_record(
                        em,
                        row_dict,
                        lookup_index=self._lookup_index,
                        treat_false_as_null=em_false,
                    )
                    observed.setdefault(tname, []).append(proj)
            return observed

        seen: set[tuple[str, str]] = set()
        for ch in diff_changes:
            tname = str(ch.get("object_type") or "")
            oid = str(ch.get("object_id") or "")
            if (tname, oid) in seen:
                continue
            seen.add((tname, oid))
            cls = self.spec.get_object_type(tname)
            if cls.is_fully_owned():
                continue
            for em in self._ems_by_type.get(tname) or ():
                pk_col = self._pk_col[em.source_table]
                raw_pk = self._raw_pk(em, oid)
                row = self.conn.execute(
                    f'SELECT * FROM "{em.source_table}" WHERE "{pk_col}"=?',
                    (raw_pk,),
                ).fetchone()
                if row is None:
                    continue
                row_dict = {k: _sanitize_sql_value(row[k]) for k in row.keys()}
                em_false = (
                    em.treat_false_as_null
                    if em.treat_false_as_null is not None
                    else self.adapter_mapping.treat_false_as_null
                )
                projected = project_single_record(
                    em,
                    row_dict,
                    lookup_index=self._lookup_index,
                    treat_false_as_null=em_false,
                )
                observed.setdefault(tname, []).append(projected)
                break
        return observed

    def as_source_bridge(self) -> SourceBridge:
        return SourceBridge(actuate=self.actuate, observe=self.observe)


def _row_matches_filter_compat(row: dict[str, Any], flt: Any) -> bool:
    from .induction.phenomenon import _row_matches_filter

    return _row_matches_filter(row, flt)


def _filter_alignment_errors_for_design(
    effective_bp: DeclarativeBlueprint,
    mapping: AdapterMappingSpec,
    align_errors: list[str],
) -> list[str]:
    """Allow non-status state fields declared in ActionExecutionBinding while validating field existence."""
    ot_by_name = {ot.name: ot for ot in effective_bp.object_types}
    binding_by_action = {b.action_name: b for b in mapping.action_bindings}
    out: list[str] = []

    for err in align_errors:
        if err.startswith("INVALID_BINDING_TARGET_STATE:"):
            suppress = False
            for act_name, binding in binding_by_action.items():
                if f"action_binding {act_name!r}" in err:
                    ot = ot_by_name.get(binding.target_object_type)
                    if (
                        ot is not None
                        and binding.expected_state_field
                        and binding.expected_state_field != ot.status_field
                        and any(f.name == binding.expected_state_field for f in ot.fields)
                    ):
                        suppress = True
                    break
            if suppress:
                continue
        out.append(err)

    for binding in mapping.action_bindings:
        ot = ot_by_name.get(binding.target_object_type)
        if ot is not None and binding.expected_state_field:
            if not any(f.name == binding.expected_state_field for f in ot.fields):
                msg = (
                    f"UNKNOWN_BINDING_STATE_FIELD: action_binding {binding.action_name!r} "
                    f"references missing field {binding.expected_state_field!r} on {ot.name}"
                )
                if msg not in out:
                    out.append(msg)
    return out


class _LoadedDeviceContext:
    def __init__(
        self,
        *,
        spec: DesignDeviceSpec,
        mapping: AdapterMappingSpec,
        profile: ExternalPhenomenonProfile,
        db_path: Path | None,
        handled_actions: tuple[str, ...],
        owned_object_types: tuple[str, ...],
        shared_object_types: tuple[str, ...],
        driver_path: Path | None = None,
        driver_factory: Callable[[Mapping[str, Any], AdapterMappingSpec], SourceBridge] | None = None,
        endpoint_config: dict[str, Any] | None = None,
        reset_fn: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.spec = spec
        self.mapping = mapping
        self.profile = profile
        self.db_path = db_path
        self.handled_actions = handled_actions
        self.owned_object_types = owned_object_types
        self.shared_object_types = shared_object_types
        self.driver_path = driver_path
        self.driver_factory = driver_factory
        self.endpoint_config = dict(endpoint_config or {})
        self.reset_fn = reset_fn
        self.projected_objects: list[dict[str, Any]] = []


def audit_thin_driver_ast(
    driver_path: Path | str,
    *,
    forbidden_literals: Sequence[str] = (),
    max_lines: int = 350,
) -> list[str]:
    """Fail-closed static AST security & anti-cheat scanner for external thin protocol drivers."""
    resolved = Path(driver_path).resolve()
    if not resolved.is_file():
        return [f"DRIVER_FILE_NOT_FOUND: thin driver file not found: {resolved}"]

    source = resolved.read_text(encoding="utf-8")
    issues: list[str] = []
    line_count = len(source.splitlines())
    if max_lines > 0 and line_count > max_lines:
        issues.append(
            f"DRIVER_EXCEEDS_LINE_BUDGET: thin driver has {line_count} lines (max {max_lines})"
        )

    try:
        tree = ast.parse(source, filename=str(resolved))
    except SyntaxError as exc:
        issues.append(f"DRIVER_SYNTAX_ERROR: line {exc.lineno}: {exc.msg}")
        return issues

    clean_literals = [str(lit).strip() for lit in forbidden_literals if str(lit).strip()]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_THIN_DRIVER_MODULES:
                    issues.append(
                        f"FORBIDDEN_DRIVER_IMPORT: line {node.lineno}: import {alias.name!r} is not allowed"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level > 0:
                issues.append(
                    f"FORBIDDEN_DRIVER_IMPORT: line {node.lineno}: relative import {mod!r} is forbidden"
                )
            elif mod in _ALLOWED_THIN_DRIVER_ROOT_EXPORTS:
                allowed_syms = _ALLOWED_THIN_DRIVER_ROOT_EXPORTS[mod]
                for alias in node.names:
                    if alias.name == "*" or alias.name not in allowed_syms:
                        issues.append(
                            f"FORBIDDEN_DRIVER_IMPORT: line {node.lineno}: "
                            f"from {mod} import {alias.name!r} is not allowed"
                        )
            elif mod not in _ALLOWED_THIN_DRIVER_MODULES:
                issues.append(
                    f"FORBIDDEN_DRIVER_IMPORT: line {node.lineno}: from {mod!r} import ... is not allowed"
                )
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                if fn.id in _FORBIDDEN_DYN_CALL_NAMES:
                    issues.append(
                        f"FORBIDDEN_DYNAMIC_EXEC: line {node.lineno}: call to {fn.id}() is forbidden"
                    )
                elif fn.id == "getattr" and len(node.args) >= 2:
                    attr_arg = node.args[1]
                    if isinstance(attr_arg, ast.Constant) and attr_arg.value in _FORBIDDEN_GETATTR_TARGETS:
                        issues.append(
                            f"FORBIDDEN_DYNAMIC_EXEC: line {node.lineno}: getattr(..., {attr_arg.value!r}) is forbidden"
                        )
                    elif (
                        isinstance(node.args[0], ast.Name)
                        and node.args[0].id in {"__builtins__", "builtins", "os"}
                    ):
                        issues.append(
                            f"FORBIDDEN_DYNAMIC_EXEC: line {node.lineno}: getattr on {node.args[0].id!r} is forbidden"
                        )
                elif fn.id == "open":
                    mode_nodes = [
                        kw.value for kw in node.keywords if kw.arg == "mode"
                    ]
                    if len(node.args) >= 2:
                        mode_nodes.append(node.args[1])
                    for mnode in mode_nodes:
                        if not isinstance(mnode, ast.Constant) or not isinstance(mnode.value, str):
                            issues.append(
                                f"FORBIDDEN_FILESYSTEM_WRITE: line {node.lineno}: non-constant open() mode"
                            )
                        elif any(ch in mnode.value for ch in ("w", "a", "x", "+")):
                            issues.append(
                                f"FORBIDDEN_FILESYSTEM_WRITE: line {node.lineno}: write open() mode {mnode.value!r} is forbidden"
                            )
            elif isinstance(fn, ast.Attribute):
                if fn.attr in _FORBIDDEN_DYN_ATTRS or fn.attr.startswith(("spawn", "execv", "execl")):
                    issues.append(
                        f"FORBIDDEN_DYNAMIC_EXEC: line {node.lineno}: call to .{fn.attr}() is forbidden"
                    )
                elif fn.attr in _FORBIDDEN_FS_WRITE_METHODS:
                    issues.append(
                        f"FORBIDDEN_FILESYSTEM_WRITE: line {node.lineno}: filesystem write call .{fn.attr}() is forbidden"
                    )
                elif fn.attr == "open":
                    mode_nodes = [kw.value for kw in node.keywords if kw.arg == "mode"]
                    if node.args:
                        first = node.args[0]
                        if (
                            isinstance(fn.value, ast.Name)
                            and fn.value.id == "io"
                            and len(node.args) >= 2
                        ):
                            mode_nodes.append(node.args[1])
                        elif (
                            isinstance(first, ast.Constant)
                            and isinstance(first.value, str)
                            and any(ch in first.value for ch in ("w", "a", "x", "+"))
                        ):
                            mode_nodes.append(first)
                    for mnode in mode_nodes:
                        if not isinstance(mnode, ast.Constant) or not isinstance(mnode.value, str):
                            issues.append(
                                f"FORBIDDEN_FILESYSTEM_WRITE: line {node.lineno}: non-constant open() mode"
                            )
                        elif any(ch in mnode.value for ch in ("w", "a", "x", "+")):
                            issues.append(
                                f"FORBIDDEN_FILESYSTEM_WRITE: line {node.lineno}: write open() mode {mnode.value!r} is forbidden"
                            )
        elif isinstance(node, ast.Name) and node.id == "__import__":
            issues.append(
                f"FORBIDDEN_DYNAMIC_EXEC: line {node.lineno}: referencing '__import__' is forbidden"
            )
        elif clean_literals and isinstance(node, ast.Constant) and isinstance(node.value, str):
            sval = node.value
            sval_low = sval.lower()
            for lit in clean_literals:
                matched = lit in sval or (len(lit) >= 4 and lit.lower() in sval_low)
                if matched:
                    issues.append(
                        f"FORBIDDEN_HARDCODED_SAMPLE: line {node.lineno}: forbidden literal {lit!r} found in string constant"
                    )

    return issues


def _load_thin_driver_factory(
    driver_path: Path,
) -> Callable[[Mapping[str, Any], AdapterMappingSpec], SourceBridge]:
    resolved = Path(driver_path).resolve()
    stat = resolved.stat()
    mod_name = f"_oo_thin_drv_{abs(hash((str(resolved), stat.st_mtime_ns, stat.st_size)))}"
    mod_spec = importlib.util.spec_from_file_location(mod_name, str(resolved))
    if mod_spec is None or mod_spec.loader is None:
        raise InvariantError(
            f"unable to load thin driver module {resolved}",
            code="INVALID_DRIVER_MODULE",
        )
    mod = importlib.util.module_from_spec(mod_spec)
    sys.modules[mod_name] = mod
    try:
        mod_spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise

    factory = getattr(mod, "build_device_bridge", None)
    if not callable(factory):
        raise InvariantError(
            f"thin driver {resolved.name!r} must expose callable build_device_bridge(endpoint_config, mapping)",
            code="MISSING_DRIVER_FACTORY",
        )
    sig = inspect.signature(factory)
    pos_params = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    if not has_varargs and len(pos_params) < 2:
        raise InvariantError(
            f"build_device_bridge in {resolved.name!r} must accept (endpoint_config, mapping)",
            code="INVALID_DRIVER_SIGNATURE",
        )
    return factory


def _load_reset_hook(
    hook_spec: str,
    *,
    base_dir: Path | None = None,
) -> Callable[[Mapping[str, Any]], None]:
    if ":" not in hook_spec:
        raise ValueError(f"reset_hook {hook_spec!r} must be 'path.py:callable' or 'module:callable'")
    target_mod, attr_name = hook_spec.split(":", 1)
    candidate_path = _resolve_existing_path(target_mod, base_dir=base_dir)
    if candidate_path.is_file() or target_mod.endswith(".py"):
        if not candidate_path.is_file():
            raise FileNotFoundError(f"reset_hook file not found: {target_mod}")
        stat = candidate_path.stat()
        mod_name = f"_oo_reset_hook_{abs(hash((str(candidate_path), stat.st_mtime_ns)))}"
        mod_spec = importlib.util.spec_from_file_location(mod_name, str(candidate_path))
        if mod_spec is None or mod_spec.loader is None:
            raise ImportError(f"cannot load reset_hook module {candidate_path}")
        mod = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_name] = mod
        mod_spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(target_mod)
    fn = getattr(mod, attr_name, None)
    if not callable(fn):
        raise ValueError(f"reset_hook {hook_spec!r} does not resolve to a callable")
    return fn


def _build_declared_profile(
    mapping: AdapterMappingSpec,
    *,
    source_kind: str,
    endpoint_label: str,
) -> ExternalPhenomenonProfile:
    tables = sorted(
        {
            *(em.source_table for em in mapping.entity_mappings if em.source_table),
            *(ab.external_table for ab in mapping.action_bindings if ab.external_table),
        }
    )
    entities = [
        ExternalEntityProfile(table_name=tbl, row_count=1, fields=[], sample_rows=[])
        for tbl in tables
    ]
    return ExternalPhenomenonProfile(
        source_kind=source_kind,
        source_locator=endpoint_label or "protocol://endpoint",
        entities=entities,
    )


def _observe_payload_to_m0_objects(
    obs_objects: Mapping[str, Sequence[Mapping[str, Any]]],
    spec: OntologySpec,
    *,
    validate_models: bool = True,
) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for tname, rows in obs_objects.items():
        if tname not in spec.object_types:
            if validate_models:
                raise InvariantError(
                    f"thin driver observe() emitted unknown object type {tname!r}",
                    code="UNKNOWN_OBSERVED_OBJECT_TYPE",
                )
            continue
        cls = spec.get_object_type(tname)
        pk_field = cls.primary_key_name()
        for raw_row in rows:
            row_dict = dict(raw_row)
            if validate_models:
                inst = cls(**row_dict)
                oid = inst.primary_key_value()
                data = inst.to_properties()
            else:
                oid = str(row_dict.get(pk_field) or "")
                data = row_dict
            key = (tname, oid)
            if key in seen_keys:
                raise InvariantError(
                    f"thin driver observe() emitted duplicate canonical key {tname}:{oid!r}",
                    code="DUPLICATE_OBSERVED_OBJECT",
                )
            seen_keys.add(key)
            objects.append({"object_type": tname, "object_id": oid, "data": data})
    return objects


def _prepare_design_context(
    *,
    blueprints: Sequence[str],
    model_id: str | None = None,
    overlay: str | None = None,
    seed: str | None = None,
    devices: Sequence[str] = (),
    resolution_spec: str | None = None,
    decisions: str | None = None,
    forbidden_literals: Sequence[str] = (),
    max_driver_lines: int | None = None,
    reset_hooks: Mapping[str, Callable[[Mapping[str, Any]], None]] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    error_codes: list[str] = []
    device_reports: list[dict[str, Any]] = []
    resolution_summary: dict[str, Any] | None = None

    # 1. Blueprint composition + overlay + compilation
    try:
        loaded_bps = [_load_blueprint_arg(bp_arg) for bp_arg in blueprints]
        if not loaded_bps:
            raise InvariantError("at least one --blueprint is required", code="EMPTY_OBJECT_TYPES")
        if len(loaded_bps) == 1:
            base_bp = copy.deepcopy(loaded_bps[0])
            if model_id:
                base_bp.model_id = model_id
        else:
            target_mid = model_id or f"{loaded_bps[0].model_id}_composed"
            base_bp = compose_blueprints(loaded_bps, model_id=target_mid, version=loaded_bps[0].version)

        if overlay:
            ov_path = Path(overlay)
            if not ov_path.is_file():
                raise FileNotFoundError(f"overlay file not found: {overlay}")
            ov_spec = CustomerOverlaySpec.model_validate(_load_json_file(ov_path))
            effective_bp = apply_overlay(base_bp, ov_spec)
        else:
            effective_bp = base_bp

        seed_obj = _load_seed_arg(seed)
        compiled_spec = compile_blueprint(effective_bp, seed_ontology=seed_obj)
        bp_fp = blueprint_fingerprint(effective_bp)
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        _add_error(errors, error_codes, f"{code}: {exc}", code=str(code))
        return {
            "ok": False,
            "errors": errors,
            "error_codes": error_codes,
            "blueprint_fingerprint": None,
            "roles": [],
            "effective_bp": None,
            "compiled_spec": None,
            "seed_obj": None,
            "device_contexts": [],
            "device_reports": device_reports,
            "resolution_summary": resolution_summary,
            "merged_m0": [],
            "registry_devices": [],
        }

    # 2. Load optional EntityResolutionSpec & Decisions
    res_spec_obj: EntityResolutionSpec | None = None
    decision_objs: list[EntityResolutionDecision] = []
    try:
        if resolution_spec:
            rp = Path(resolution_spec)
            if not rp.is_file():
                raise FileNotFoundError(f"resolution spec file not found: {resolution_spec}")
            res_spec_obj = EntityResolutionSpec.model_validate(_load_json_file(rp))
        if decisions:
            dp = Path(decisions)
            if not dp.is_file():
                raise FileNotFoundError(f"decisions file not found: {decisions}")
            raw_decs = _load_json_file(dp)
            if not isinstance(raw_decs, list):
                raise ValueError("decisions file must contain a JSON array")
            decision_objs = [EntityResolutionDecision.model_validate(item) for item in raw_decs]
    except Exception as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        _add_error(errors, error_codes, f"{code}: {exc}", code=str(code))
        return {
            "ok": False,
            "errors": errors,
            "error_codes": error_codes,
            "blueprint_fingerprint": bp_fp,
            "roles": list(effective_bp.roles),
            "effective_bp": effective_bp,
            "compiled_spec": compiled_spec,
            "seed_obj": seed_obj,
            "device_contexts": [],
            "device_reports": device_reports,
            "resolution_summary": resolution_summary,
            "merged_m0": [],
            "registry_devices": [],
        }

    # 3. Load devices, read-only SQLite profiles, and AdapterMappingSpecs
    loaded_devices: list[_LoadedDeviceContext] = []
    for dev_arg in devices:
        dev_label = dev_arg
        try:
            dev_spec, dev_base_dir = _load_device_arg(dev_arg)
            dev_label = dev_spec.device_id
            map_path = _resolve_existing_path(dev_spec.mapping_path, base_dir=dev_base_dir)
            if not map_path.is_file():
                raise FileNotFoundError(f"mapping file not found: {dev_spec.mapping_path}")
            mapping = AdapterMappingSpec.model_validate(_load_json_file(map_path))
            include_tbls = [em.source_table for em in mapping.entity_mappings if em.source_table]

            resolved_db: Path | None = None
            if dev_spec.db_path:
                resolved_db = _resolve_existing_path(dev_spec.db_path, base_dir=dev_base_dir)

            resolved_driver_path: Path | None = None
            driver_factory: Callable[[Mapping[str, Any], AdapterMappingSpec], SourceBridge] | None = None
            resolved_ep_config: dict[str, Any] = {}
            resolved_reset_fn: Callable[[Mapping[str, Any]], None] | None = (
                dict(reset_hooks).get(dev_spec.device_id) if reset_hooks else None
            )

            if dev_spec.driver_module_path:
                resolved_driver_path = _resolve_existing_path(
                    dev_spec.driver_module_path, base_dir=dev_base_dir
                )
                eff_forbidden = list(
                    dict.fromkeys([*dev_spec.forbidden_literals, *forbidden_literals])
                )
                eff_max_lines = (
                    max_driver_lines
                    if max_driver_lines is not None
                    else dev_spec.max_driver_lines
                )
                ast_issues = audit_thin_driver_ast(
                    resolved_driver_path,
                    forbidden_literals=eff_forbidden,
                    max_lines=eff_max_lines,
                )
                if ast_issues:
                    for ast_msg in ast_issues:
                        _add_error(
                            errors,
                            error_codes,
                            f"[{dev_spec.device_id}] {ast_msg}",
                            code=_extract_error_code(ast_msg),
                        )
                    continue

                driver_factory = _load_thin_driver_factory(resolved_driver_path)
                if dev_spec.endpoint_config_path:
                    ep_path = _resolve_existing_path(
                        dev_spec.endpoint_config_path, base_dir=dev_base_dir
                    )
                    if not ep_path.is_file():
                        raise FileNotFoundError(
                            f"endpoint_config_path not found: {dev_spec.endpoint_config_path}"
                        )
                    loaded_ep = _load_json_file(ep_path)
                    if not isinstance(loaded_ep, Mapping):
                        raise ValueError("endpoint_config_path must contain a JSON object")
                    resolved_ep_config.update(dict(loaded_ep))
                if dev_spec.endpoint_config:
                    resolved_ep_config.update(copy.deepcopy(dev_spec.endpoint_config))

                if resolved_reset_fn is None and dev_spec.reset_hook:
                    resolved_reset_fn = _load_reset_hook(
                        dev_spec.reset_hook, base_dir=dev_base_dir
                    )

            if dev_spec.profile_path:
                prof_path = _resolve_existing_path(dev_spec.profile_path, base_dir=dev_base_dir)
                profile = ExternalPhenomenonProfile.model_validate(_load_json_file(prof_path))
            elif resolved_db is not None:
                profile = scout_readonly_sqlite_profile(
                    resolved_db,
                    source_kind=mapping.source_kind or dev_spec.kind,
                    include_tables=include_tbls,
                )
            elif driver_factory is not None:
                ep_label = dev_spec.endpoint or str(
                    resolved_ep_config.get("url") or f"{dev_spec.kind}://{dev_spec.device_id}"
                )
                profile = _build_declared_profile(
                    mapping,
                    source_kind=mapping.source_kind or dev_spec.kind,
                    endpoint_label=ep_label,
                )
            else:
                raise ValueError(
                    f"device {dev_spec.device_id!r} requires db_path, profile_path, or driver_module_path"
                )

            handled = tuple(
                dev_spec.handled_actions
                if dev_spec.handled_actions
                else [b.action_name for b in mapping.action_bindings]
            )
            if dev_spec.owned_object_types or dev_spec.shared_object_types:
                owned = tuple(dev_spec.owned_object_types)
                shared = tuple(dev_spec.shared_object_types)
            else:
                res_target = res_spec_obj.target_object_type if res_spec_obj is not None else ""
                mapped_ots = [em.target_object_type for em in mapping.entity_mappings]
                shared = tuple(t for t in dict.fromkeys(mapped_ots) if t == res_target)
                owned = tuple(t for t in dict.fromkeys(mapped_ots) if t != res_target)

            loaded_devices.append(
                _LoadedDeviceContext(
                    spec=dev_spec,
                    mapping=mapping,
                    profile=profile,
                    db_path=resolved_db,
                    handled_actions=handled,
                    owned_object_types=owned,
                    shared_object_types=shared,
                    driver_path=resolved_driver_path,
                    driver_factory=driver_factory,
                    endpoint_config=resolved_ep_config,
                    reset_fn=resolved_reset_fn,
                )
            )
        except (ValidationError, Exception) as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            _add_error(errors, error_codes, f"{code}: device {dev_label!r}: {exc}", code=str(code))

    # 4. Run Crosswalk Entity Resolution (if configured) and immutably inject aliases
    if res_spec_obj is not None and loaded_devices and not errors:
        try:
            batches: list[SourceEntityBatch] = []
            for dctx in loaded_devices:
                target_em = dctx.mapping.get_entity_mapping(res_spec_obj.target_object_type)
                if target_em is None:
                    continue
                ent_prof = next(
                    (e for e in dctx.profile.entities if e.table_name == target_em.source_table),
                    None,
                )
                if ent_prof is None:
                    continue
                pk_field = target_em.id_transform.source_field
                if not pk_field:
                    pk_field = next(
                        (f.name for f in ent_prof.fields if f.label == "PRIMARY_KEY"),
                        "id",
                    )
                field_map = {
                    tf.target_field: tf.source_field
                    for tf in target_em.field_transforms
                    if tf.source_field
                }
                batches.append(
                    SourceEntityBatch(
                        source_id=dctx.spec.device_id,
                        source_table=target_em.source_table,
                        pk_field=pk_field,
                        records=list(ent_prof.sample_rows),
                        field_map=field_map,
                    )
                )

            if batches:
                er_report = resolve_entities(res_spec_obj, batches)
                if decision_objs:
                    cw_updated = apply_resolution_decisions(er_report.crosswalk, decision_objs)
                    er_report = resolve_entities(res_spec_obj, batches, existing_crosswalk=cw_updated)

                fk_fields: list[tuple[str, str]] = [
                    (ot.name, rel.field)
                    for ot in effective_bp.object_types
                    for rel in ot.relations
                    if rel.target_type == res_spec_obj.target_object_type
                ]
                for dctx in loaded_devices:
                    dctx.mapping = inject_crosswalk_into_mapping(
                        dctx.mapping,
                        source_id=dctx.spec.device_id,
                        crosswalk=er_report.crosswalk,
                        target_object_type=res_spec_obj.target_object_type,
                        foreign_key_fields=fk_fields,
                    )
                resolution_summary = {
                    "target_object_type": res_spec_obj.target_object_type,
                    "stats": dict(er_report.stats),
                    "crosswalk_entries": [
                        {
                            "canonical_id": entry.canonical_id,
                            "match_tier": entry.match_tier,
                            "confidence": entry.confidence,
                            "members": [m.model_dump(mode="json") for m in entry.members],
                            "golden_record": dict(entry.golden_record),
                        }
                        for entry in er_report.crosswalk.entries
                    ],
                    "blocked_pairs": [bp.model_dump(mode="json") for bp in er_report.blocked_pairs],
                    "ambiguous_candidates": [
                        {
                            "candidate_id": cand.candidate_id,
                            "left": cand.left.model_dump(mode="json"),
                            "right": cand.right.model_dump(mode="json"),
                            "score": cand.score,
                            "reasons": list(cand.reasons),
                        }
                        for cand in er_report.ambiguous_candidates
                    ],
                }
        except Exception as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            _add_error(errors, error_codes, f"{code}: resolution failed: {exc}", code=str(code))

    # 5. Validate DeviceDriverRegistry bus mount rules & per-device alignment/projection
    bus = DeviceDriverRegistry(spec=compiled_spec)
    dummy_bridge = SourceBridge(actuate=lambda _mv: None, observe=lambda _mv=None: {})
    per_device_m0: list[list[dict[str, Any]]] = []

    for dctx in loaded_devices:
        dev_errors: list[str] = []
        try:
            bus.mount(
                MountedDevice(
                    device_id=dctx.spec.device_id,
                    name=dctx.spec.name or dctx.spec.device_id,
                    kind=dctx.spec.kind,
                    endpoint=dctx.spec.endpoint or (
                        f"sqlite://{Path(dctx.db_path).name}" if dctx.db_path else "memory://"
                    ),
                    bridge=dummy_bridge,
                    handled_actions=dctx.handled_actions,
                    owned_object_types=dctx.owned_object_types,
                    shared_object_types=dctx.shared_object_types,
                    allow_source_revert=dctx.spec.allow_source_revert,
                )
            )
        except Exception as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            msg = f"{code}: device {dctx.spec.device_id!r} mount rejected: {exc}"
            dev_errors.append(msg)
            _add_error(errors, error_codes, msg, code=str(code))

        if (
            dctx.driver_factory is not None
            and dctx.endpoint_config
            and not dctx.spec.profile_path
            and dctx.db_path is None
        ):
            try:
                if dctx.reset_fn is not None:
                    dctx.reset_fn(copy.deepcopy(dctx.endpoint_config))
                live_bridge = dctx.driver_factory(
                    copy.deepcopy(dctx.endpoint_config),
                    dctx.mapping,
                )
                obs_objs, _ = call_observe(live_bridge, None, spec=compiled_spec)
                projected_objs = _observe_payload_to_m0_objects(
                    obs_objs, compiled_spec, validate_models=True
                )
                present_types = {str(e.get("object_type") or "") for e in projected_objs}
                for req_ot in dict.fromkeys([*dctx.owned_object_types, *dctx.shared_object_types]):
                    ot_cls = compiled_spec.get_object_type(req_ot)
                    if not ot_cls.is_fully_owned() and req_ot not in present_types:
                        msg = (
                            f"EMPTY_DRIVER_PROJECTION: thin driver observe() returned 0 records for {req_ot!r}"
                        )
                        dev_errors.append(msg)
                        _add_error(
                            errors,
                            error_codes,
                            f"[{dctx.spec.device_id}] {msg}",
                            code="EMPTY_DRIVER_PROJECTION",
                        )
            except Exception as exc:
                code = getattr(exc, "code", None) or type(exc).__name__
                msg = f"{code}: live thin driver observe() failed: {exc}"
                dev_errors.append(msg)
                _add_error(
                    errors,
                    error_codes,
                    f"[{dctx.spec.device_id}] {msg}",
                    code=str(code),
                )
                projected_objs = []
        else:
            projected_objs, proj_errors = project_external_phenomenon(dctx.profile, dctx.mapping)
            for pe in proj_errors:
                dev_errors.append(pe)
                _add_error(
                    errors,
                    error_codes,
                    f"[{dctx.spec.device_id}] {pe}",
                    code=_extract_error_code(pe),
                )

        req_ots = list(dict.fromkeys([*dctx.owned_object_types, *dctx.shared_object_types]))
        raw_align_errors = _validate_decompiled_alignment(
            effective_bp=effective_bp,
            external_profile=dctx.profile,
            mapping=dctx.mapping,
            projected_objects=projected_objs,
            required_object_types=req_ots,
            required_actions=dctx.handled_actions,
        )
        align_errors = _filter_alignment_errors_for_design(
            effective_bp,
            dctx.mapping,
            raw_align_errors,
        )
        for ae in align_errors:
            dev_errors.append(ae)
            _add_error(errors, error_codes, f"[{dctx.spec.device_id}] {ae}", code=_extract_error_code(ae))

        dctx.projected_objects = projected_objs
        if projected_objs:
            per_device_m0.append(projected_objs)

        device_reports.append(
            {
                "device_id": dctx.spec.device_id,
                "name": dctx.spec.name or dctx.spec.device_id,
                "kind": dctx.spec.kind,
                "table_count": len(dctx.profile.entities),
                "projected_count": len(projected_objs),
                "handled_actions": list(dctx.handled_actions),
                "owned_object_types": list(dctx.owned_object_types),
                "shared_object_types": list(dctx.shared_object_types),
                "errors": dev_errors,
            }
        )

    merged_m0 = merge_projected_phenomena(per_device_m0) if per_device_m0 else []

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "error_codes": error_codes,
        "blueprint_fingerprint": bp_fp,
        "roles": list(effective_bp.roles),
        "effective_bp": effective_bp,
        "compiled_spec": compiled_spec,
        "seed_obj": seed_obj,
        "device_contexts": loaded_devices,
        "device_reports": device_reports,
        "resolution_summary": resolution_summary,
        "merged_m0": merged_m0,
        "registry_devices": bus.list_devices(),
    }


class _CountingThinBridge:
    """Adapter wrapper counting external write attempts and marking dirty device state."""

    def __init__(
        self,
        inner: SourceBridge,
        on_actuate: Callable[[], None],
        spec: OntologySpec,
    ) -> None:
        self._inner = inner
        self._on_actuate = on_actuate
        self._spec = spec
        self.write_count = 0

    def close(self) -> None:
        close_fn = getattr(self._inner, "close", None)
        if callable(close_fn):
            close_fn()

    def as_source_bridge(self) -> SourceBridge:
        def _act(move: dict[str, Any]) -> Any:
            self._on_actuate()
            self.write_count += 1
            return self._inner.actuate(move)

        def _obs(move: dict[str, Any] | None = None) -> Any:
            objs, links = call_observe(self._inner, move, spec=self._spec)
            if links is not None:
                return objs, links
            return objs

        return SourceBridge(
            actuate=_act,
            observe=_obs,
            allow_source_revert=self._inner.allow_source_revert,
        )


class _MultiDeviceSandboxAdapter:
    """Isolated multi-device ExternalSandboxAdapter backed by DeviceDriverRegistry."""

    def __init__(
        self,
        *,
        device_contexts: Sequence[_LoadedDeviceContext],
        work_root: Path,
        spec: OntologySpec,
        resolution_spec_obj: EntityResolutionSpec | None = None,
        decision_objs: Sequence[EntityResolutionDecision] = (),
        effective_bp: DeclarativeBlueprint | None = None,
    ) -> None:
        self.device_contexts = list(device_contexts)
        self.work_root = work_root
        self.spec = spec
        self.resolution_spec_obj = resolution_spec_obj
        self.decision_objs = list(decision_objs)
        self.effective_bp = effective_bp
        self._baseline_dbs: dict[str, Path] = {}
        self._work_dbs: dict[str, Path] = {}
        self._dirty_thin_devices: dict[str, bool] = {}
        self._open_bridges: list[Any] = []
        self._historical_writes = 0

        for idx, dctx in enumerate(self.device_contexts):
            if dctx.db_path is not None:
                base_copy = self.work_root / f"baseline_{idx}_{dctx.spec.device_id}.sqlite3"
                work_copy = self.work_root / f"work_{idx}_{dctx.spec.device_id}.sqlite3"
                shutil.copy2(dctx.db_path, base_copy)
                os.chmod(base_copy, 0o600)
                shutil.copy2(base_copy, work_copy)
                os.chmod(work_copy, 0o600)
                self._baseline_dbs[dctx.spec.device_id] = base_copy
                self._work_dbs[dctx.spec.device_id] = work_copy
            elif dctx.driver_factory is not None and dctx.endpoint_config:
                self._dirty_thin_devices[dctx.spec.device_id] = False
            else:
                raise InvariantError(
                    f"device {dctx.spec.device_id!r} requires db_path or non-empty endpoint_config",
                    code="MISSING_ENDPOINT_CONFIG",
                )

    def _close_bridges(self) -> None:
        while self._open_bridges:
            bridge = self._open_bridges.pop()
            self._historical_writes += bridge.write_count
            bridge.close()

    def close(self) -> None:
        self._close_bridges()

    def reset(self) -> None:
        self._close_bridges()
        for dctx in self.device_contexts:
            dev_id = dctx.spec.device_id
            if dev_id in self._work_dbs:
                wdb = self._work_dbs[dev_id]
                for suffix in ("", "-wal", "-shm", "-journal"):
                    Path(f"{wdb}{suffix}").unlink(missing_ok=True)
                shutil.copy2(self._baseline_dbs[dev_id], wdb)
                os.chmod(wdb, 0o600)
            elif self._dirty_thin_devices.get(dev_id, False):
                if dctx.reset_fn is not None:
                    dctx.reset_fn(copy.deepcopy(dctx.endpoint_config))
                self._dirty_thin_devices[dev_id] = False

    def snapshot_m0(self) -> list[dict[str, Any]]:
        per_device: list[list[dict[str, Any]]] = []
        for dctx in self.device_contexts:
            dev_id = dctx.spec.device_id
            if dev_id in self._work_dbs:
                wdb = self._work_dbs[dev_id]
                include_tbls = [em.source_table for em in dctx.mapping.entity_mappings if em.source_table]
                prof = scout_readonly_sqlite_profile(
                    wdb,
                    source_kind=dctx.mapping.source_kind or dctx.spec.kind,
                    include_tables=include_tbls,
                    max_samples_per_table=0,
                )
                proj, _ = project_external_phenomenon(
                    prof,
                    dctx.mapping,
                    require_non_empty_per_entity=False,
                )
                per_device.append(proj)
            elif dctx.driver_factory is not None:
                drv_bridge = dctx.driver_factory(
                    copy.deepcopy(dctx.endpoint_config),
                    dctx.mapping,
                )
                obs_objs, _ = call_observe(drv_bridge, None, spec=self.spec)
                per_device.append(
                    _observe_payload_to_m0_objects(
                        obs_objs, self.spec, validate_models=False
                    )
                )
        return merge_projected_phenomena(per_device) if per_device else []

    def make_source_bridge(
        self,
        spec: OntologySpec,
        adapter_mapping: AdapterMappingSpec,
    ) -> SourceBridge:
        _ = adapter_mapping
        self._close_bridges()
        bus = DeviceDriverRegistry(spec=spec)
        for dctx in self.device_contexts:
            dev_id = dctx.spec.device_id
            if dctx.driver_factory is not None and (dev_id not in self._work_dbs or dctx.endpoint_config):
                ep_cfg = copy.deepcopy(dctx.endpoint_config)
                if dev_id in self._work_dbs and "db_path" not in ep_cfg:
                    ep_cfg["db_path"] = str(self._work_dbs[dev_id])
                inner_bridge = dctx.driver_factory(ep_cfg, dctx.mapping)

                def _mark_dirty(target_id: str = dev_id) -> None:
                    self._dirty_thin_devices[target_id] = True

                wrapped = _CountingThinBridge(inner_bridge, _mark_dirty, spec)
                self._open_bridges.append(wrapped)
                mount_bridge = wrapped.as_source_bridge()
                mount_ep = dctx.spec.endpoint or str(
                    ep_cfg.get("url") or f"{dctx.spec.kind}://{dev_id}"
                )
            else:
                wdb = self._work_dbs[dev_id]
                include_tbls = [em.source_table for em in dctx.mapping.entity_mappings if em.source_table]
                prof = scout_readonly_sqlite_profile(
                    wdb,
                    source_kind=dctx.mapping.source_kind or dctx.spec.kind,
                    include_tables=include_tbls,
                    max_samples_per_table=0,
                )
                drv = DeclarativeSQLiteDriverBridge(
                    wdb,
                    spec=spec,
                    profile=prof,
                    adapter_mapping=dctx.mapping,
                )
                self._open_bridges.append(drv)
                mount_bridge = drv.as_source_bridge()
                mount_ep = f"sqlite://{wdb.name}"

            bus.mount(
                MountedDevice(
                    device_id=dev_id,
                    name=dctx.spec.name or dev_id,
                    kind=dctx.spec.kind,
                    endpoint=mount_ep,
                    bridge=mount_bridge,
                    handled_actions=dctx.handled_actions,
                    owned_object_types=dctx.owned_object_types,
                    shared_object_types=dctx.shared_object_types,
                    allow_source_revert=dctx.spec.allow_source_revert or mount_bridge.allow_source_revert,
                )
            )
        return bus.as_source_bridge()

    def external_write_count(self) -> int:
        return self._historical_writes + sum(b.write_count for b in self._open_bridges)


def run_design_check(
    *,
    blueprints: Sequence[str],
    model_id: str | None = None,
    overlay: str | None = None,
    seed: str | None = None,
    devices: Sequence[str] = (),
    resolution_spec: str | None = None,
    decisions: str | None = None,
    forbidden_literals: Sequence[str] = (),
    max_driver_lines: int | None = None,
    reset_hooks: Mapping[str, Callable[[Mapping[str, Any]], None]] | None = None,
) -> dict[str, Any]:
    """Execute deterministic Design-Time compilation and alignment checks (``javac``)."""
    prep = _prepare_design_context(
        blueprints=blueprints,
        model_id=model_id,
        overlay=overlay,
        seed=seed,
        devices=devices,
        resolution_spec=resolution_spec,
        decisions=decisions,
        forbidden_literals=forbidden_literals,
        max_driver_lines=max_driver_lines,
        reset_hooks=reset_hooks,
    )
    return {
        "ok": prep["ok"],
        "command": "check",
        "errors": prep["errors"],
        "error_codes": prep["error_codes"],
        "blueprint_fingerprint": prep["blueprint_fingerprint"],
        "roles": prep["roles"],
        "device_reports": prep["device_reports"],
        "resolution_summary": prep["resolution_summary"],
    }


def run_design_verify(
    *,
    blueprints: Sequence[str],
    model_id: str | None = None,
    overlay: str | None = None,
    seed: str | None = None,
    devices: Sequence[str] = (),
    resolution_spec: str | None = None,
    decisions: str | None = None,
    min_action_coverage: float = 0.8,
    min_transition_coverage: float = 0.8,
    max_waves: int = 3,
    target_actions: Sequence[str] | None = None,
    forbidden_literals: Sequence[str] = (),
    max_driver_lines: int | None = None,
    reset_hooks: Mapping[str, Callable[[Mapping[str, Any]], None]] | None = None,
) -> dict[str, Any]:
    """Execute full Crucible + Multi-Device DifferentialFalsifier referee checks (``JUnit``)."""
    prep = _prepare_design_context(
        blueprints=blueprints,
        model_id=model_id,
        overlay=overlay,
        seed=seed,
        devices=devices,
        resolution_spec=resolution_spec,
        decisions=decisions,
        forbidden_literals=forbidden_literals,
        max_driver_lines=max_driver_lines,
        reset_hooks=reset_hooks,
    )
    errors = list(prep["errors"])
    error_codes = list(prep["error_codes"])
    if not prep["ok"] or prep["effective_bp"] is None or prep["compiled_spec"] is None:
        return {
            "ok": False,
            "command": "verify",
            "verdict": "FALSIFIED",
            "errors": errors,
            "error_codes": error_codes,
            "blueprint_fingerprint": prep["blueprint_fingerprint"],
            "roles": prep["roles"],
            "device_reports": prep["device_reports"],
            "mounted_devices": prep["registry_devices"],
            "resolution_summary": prep["resolution_summary"],
            "crucible_passed": False,
            "crucible_feedback": "\n".join(errors),
            "falsification": None,
        }

    effective_bp: DeclarativeBlueprint = prep["effective_bp"]
    compiled_spec: OntologySpec = prep["compiled_spec"]
    seed_obj: SeedOntology | None = prep["seed_obj"]
    device_contexts: list[_LoadedDeviceContext] = prep["device_contexts"]
    merged_m0: list[dict[str, Any]] = prep["merged_m0"]

    combined_mapping = AdapterMappingSpec(
        mapping_id="design_combined_mapping",
        source_kind="multi_device",
        entity_mappings=[
            em
            for dctx in device_contexts
            for em in dctx.mapping.entity_mappings
        ],
        action_bindings=[
            ab
            for dctx in device_contexts
            for ab in dctx.mapping.action_bindings
        ],
    )

    crucible_report = SymbolicCrucible().evaluate(
        effective_bp,
        CrucibleContext(
            mode="genesis",
            m0_objects=merged_m0,
            seed_ontology=seed_obj,
            adapter_mapping=combined_mapping,
        ),
    )
    crucible_passed = bool(crucible_report.passed)
    crucible_feedback = crucible_report.diagnostic_feedback()
    if not crucible_passed:
        _add_error(errors, error_codes, crucible_feedback, code="CRUCIBLE_FAILED")
        return {
            "ok": False,
            "command": "verify",
            "verdict": "FALSIFIED",
            "errors": errors,
            "error_codes": error_codes,
            "blueprint_fingerprint": prep["blueprint_fingerprint"],
            "roles": prep["roles"],
            "device_reports": prep["device_reports"],
            "mounted_devices": prep["registry_devices"],
            "resolution_summary": prep["resolution_summary"],
            "crucible_passed": False,
            "crucible_feedback": crucible_feedback,
            "falsification": None,
        }

    if not device_contexts:
        return {
            "ok": True,
            "command": "verify",
            "verdict": "CORROBORATED",
            "errors": [],
            "error_codes": [],
            "blueprint_fingerprint": prep["blueprint_fingerprint"],
            "roles": prep["roles"],
            "device_reports": [],
            "mounted_devices": [],
            "resolution_summary": prep["resolution_summary"],
            "crucible_passed": True,
            "crucible_feedback": crucible_feedback,
            "falsification": None,
        }

    with tempfile.TemporaryDirectory(prefix="oo_design_verify_") as tmpdir:
        sandbox = _MultiDeviceSandboxAdapter(
            device_contexts=device_contexts,
            work_root=Path(tmpdir),
            spec=compiled_spec,
            effective_bp=effective_bp,
        )
        try:
            falsifier = DifferentialFalsifier(
                min_action_coverage=min_action_coverage,
                min_transition_coverage=min_transition_coverage,
                max_waves=max_waves,
            )
            fal_report = falsifier.evaluate(
                blueprint=effective_bp,
                adapter_mapping=combined_mapping,
                sandbox=sandbox,
                seed_ontology=seed_obj,
                target_actions=target_actions,
            )
            fal_dict = fal_report.to_dict()
            for ce in fal_report.counterexamples:
                _add_error(
                    errors,
                    error_codes,
                    f"{ce.kind}: ({ce.case_name}/{ce.action}) {ce.message}",
                    code=ce.kind,
                )
            ok = fal_report.verdict == "CORROBORATED" and len(errors) == 0
            return {
                "ok": ok,
                "command": "verify",
                "verdict": fal_report.verdict,
                "errors": errors,
                "error_codes": error_codes,
                "blueprint_fingerprint": prep["blueprint_fingerprint"],
                "roles": prep["roles"],
                "device_reports": prep["device_reports"],
                "mounted_devices": prep["registry_devices"],
                "resolution_summary": prep["resolution_summary"],
                "crucible_passed": True,
                "crucible_feedback": crucible_feedback,
                "falsification": fal_dict,
            }
        finally:
            sandbox.close()


def verify_thin_driver_on_hidden_world(
    *,
    blueprints: Sequence[str],
    driver_path: Path | str,
    mapping_path: Path | str | AdapterMappingSpec,
    hidden_endpoint_config: Mapping[str, Any],
    forbidden_literals: Sequence[str] = (),
    max_driver_lines: int = 350,
    model_id: str | None = None,
    overlay: str | None = None,
    seed: str | None = None,
    device_id: str = "hidden_protocol_device",
    device_kind: str = "protocol",
    owned_object_types: Sequence[str] = (),
    shared_object_types: Sequence[str] = (),
    handled_actions: Sequence[str] = (),
    allow_source_revert: bool = False,
    reset_fn: Callable[[Mapping[str, Any]], None] | None = None,
    oob_tamper_check: Callable[[Mapping[str, Any], Sequence[dict[str, Any]]], Mapping[str, Any]] | None = None,
    min_action_coverage: float = 0.8,
    min_transition_coverage: float = 0.8,
    max_waves: int = 3,
    target_actions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Pure M2 blind falsification gate injecting unseen endpoint credentials into a thin driver."""
    from .runtime import OntologyRuntime

    resolved_driver = Path(driver_path).resolve()
    ast_errors = audit_thin_driver_ast(
        resolved_driver,
        forbidden_literals=forbidden_literals,
        max_lines=max_driver_lines,
    )
    if ast_errors:
        errors: list[str] = []
        error_codes: list[str] = []
        for msg in ast_errors:
            _add_error(errors, error_codes, msg, code=_extract_error_code(msg))
        return {
            "ok": False,
            "command": "hidden_verify",
            "verdict": "FALSIFIED",
            "errors": errors,
            "error_codes": error_codes,
            "ast_errors": ast_errors,
            "blueprint_fingerprint": None,
            "roles": [],
            "device_reports": [],
            "mounted_devices": [],
            "resolution_summary": None,
            "crucible_passed": False,
            "crucible_feedback": "\n".join(ast_errors),
            "falsification": None,
            "oob_tamper_result": None,
        }

    ep_copy = copy.deepcopy(dict(hidden_endpoint_config))
    if not ep_copy:
        return {
            "ok": False,
            "command": "hidden_verify",
            "verdict": "FALSIFIED",
            "errors": ["MISSING_ENDPOINT_CONFIG: hidden_endpoint_config must not be empty"],
            "error_codes": ["MISSING_ENDPOINT_CONFIG"],
            "ast_errors": [],
            "blueprint_fingerprint": None,
            "roles": [],
            "device_reports": [],
            "mounted_devices": [],
            "resolution_summary": None,
            "crucible_passed": False,
            "crucible_feedback": "hidden_endpoint_config must not be empty",
            "falsification": None,
            "oob_tamper_result": None,
        }

    with tempfile.TemporaryDirectory(prefix="oo_hidden_verify_") as tmpdir:
        tmp_path = Path(tmpdir)
        if isinstance(mapping_path, AdapterMappingSpec):
            mapping_obj = mapping_path
            resolved_mapping_file = tmp_path / "mapping.json"
            resolved_mapping_file.write_text(
                json.dumps(mapping_obj.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        else:
            resolved_mapping_file = Path(mapping_path).resolve()
            if not resolved_mapping_file.is_file():
                raise FileNotFoundError(f"mapping file not found: {mapping_path}")
            mapping_obj = AdapterMappingSpec.model_validate(_load_json_file(resolved_mapping_file))

        dev_payload = {
            "device_id": device_id,
            "name": f"Hidden Blind Target ({device_id})",
            "kind": device_kind,
            "endpoint": str(ep_copy.get("url") or f"{device_kind}://{device_id}"),
            "mapping_path": str(resolved_mapping_file),
            "driver_module_path": str(resolved_driver),
            "endpoint_config": ep_copy,
            "forbidden_literals": list(forbidden_literals),
            "max_driver_lines": max_driver_lines,
            "owned_object_types": list(owned_object_types),
            "shared_object_types": list(shared_object_types),
            "handled_actions": list(handled_actions),
            "allow_source_revert": bool(allow_source_revert),
        }
        hidden_dev_file = tmp_path / "hidden_device.json"
        hidden_dev_file.write_text(json.dumps(dev_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        ver_res = run_design_verify(
            blueprints=blueprints,
            model_id=model_id,
            overlay=overlay,
            seed=seed,
            devices=[str(hidden_dev_file)],
            min_action_coverage=min_action_coverage,
            min_transition_coverage=min_transition_coverage,
            max_waves=max_waves,
            target_actions=target_actions,
            forbidden_literals=forbidden_literals,
            max_driver_lines=max_driver_lines,
            reset_hooks={device_id: reset_fn} if reset_fn is not None else None,
        )

    oob_result: dict[str, Any] | None = None
    errors = list(ver_res["errors"])
    error_codes = list(ver_res["error_codes"])
    ok = bool(ver_res["ok"])
    verdict = str(ver_res["verdict"])

    if ok and oob_tamper_check is not None:
        if reset_fn is not None:
            reset_fn(copy.deepcopy(ep_copy))
        prep = _prepare_design_context(
            blueprints=blueprints,
            model_id=model_id,
            overlay=overlay,
            seed=seed,
            devices=(),
        )
        compiled_spec: OntologySpec | None = prep.get("compiled_spec")
        if compiled_spec is None:
            _add_error(
                errors,
                error_codes,
                "OOB_TAMPER_SETUP_FAILED: unable to compile OntologySpec",
                code="OOB_TAMPER_SETUP_FAILED",
            )
            ok = False
            verdict = "FALSIFIED"
        else:
            driver_factory = _load_thin_driver_factory(resolved_driver)
            drv_bridge = driver_factory(copy.deepcopy(ep_copy), mapping_obj)
            eff_handled = tuple(
                handled_actions
                if handled_actions
                else [b.action_name for b in mapping_obj.action_bindings]
            )
            if owned_object_types or shared_object_types:
                eff_owned = tuple(owned_object_types)
                eff_shared = tuple(shared_object_types)
            else:
                eff_owned = tuple(dict.fromkeys(em.target_object_type for em in mapping_obj.entity_mappings))
                eff_shared = ()

            bus = DeviceDriverRegistry(spec=compiled_spec)
            bus.mount(
                MountedDevice(
                    device_id=device_id,
                    name=device_id,
                    kind=device_kind,
                    endpoint=str(ep_copy.get("url") or f"{device_kind}://{device_id}"),
                    bridge=drv_bridge,
                    handled_actions=eff_handled,
                    owned_object_types=eff_owned,
                    shared_object_types=eff_shared,
                    allow_source_revert=allow_source_revert or drv_bridge.allow_source_revert,
                )
            )
            bus_bridge = bus.as_source_bridge()
            obs_before, _ = call_observe(bus_bridge, None, spec=compiled_spec)
            m0_before = _observe_payload_to_m0_objects(
                obs_before, compiled_spec, validate_models=True
            )
            coupled_rt = OntologyRuntime(
                compiled_spec,
                db_path=":memory:",
                world_mode="coupled",
                source_bridge=bus_bridge,
            )
            try:
                type_order = {name: idx for idx, name in enumerate(compiled_spec.object_types)}
                for entry in sorted(
                    m0_before,
                    key=lambda item: type_order.get(str(item.get("object_type") or ""), 9999),
                ):
                    tname = str(entry["object_type"])
                    cls = compiled_spec.get_object_type(tname)
                    coupled_rt.put_object(cls(**entry["data"]))

                audit_actor = Actor(
                    actor_id="referee:audit", role="admin", clearance_tags={"*"}
                )
                abox_before = {
                    ot_name: {
                        obj.primary_key_value(): obj.to_properties()
                        for obj in coupled_rt.query_objects(ot_name, audit_actor)
                    }
                    for ot_name in compiled_spec.object_types
                }

                probe = dict(oob_tamper_check(copy.deepcopy(ep_copy), m0_before))
                probe_action = str(probe["action"])
                probe_params = dict(probe["params"])
                probe_role = str(probe.get("actor_role") or "admin")
                probe_actor = Actor(actor_id="referee:oob_tamper", role=probe_role)
                run_id = "WO14-OOB-TAMPER"

                caught_code: str | None = None
                caught_msg = ""
                committed_unexpectedly = False
                try:
                    submit_res = coupled_rt.submit_action(
                        run_id, probe_action, probe_params, probe_actor
                    )
                    committed_unexpectedly = submit_res.get("status") == "COMMITTED"
                except DeviceStateMismatchError as exc:
                    caught_code = getattr(exc, "code", None) or DEVICE_STATE_MISMATCH
                    caught_msg = str(exc)
                except Exception as exc:
                    caught_code = getattr(exc, "code", None) or type(exc).__name__
                    caught_msg = str(exc)

                abox_after = {
                    ot_name: {
                        obj.primary_key_value(): obj.to_properties()
                        for obj in coupled_rt.query_objects(ot_name, audit_actor)
                    }
                    for ot_name in compiled_spec.object_types
                }
                abox_unchanged = abox_after == abox_before
                audit_row = coupled_rt.get_action_run(run_id)
                audit_status = audit_row.get("status") if audit_row else None
                passed = (
                    not committed_unexpectedly
                    and caught_code == DEVICE_STATE_MISMATCH
                    and abox_unchanged
                    and audit_status == "REJECTED"
                )
                oob_result = {
                    "passed": passed,
                    "action": probe_action,
                    "params": probe_params,
                    "caught_error_code": caught_code,
                    "caught_message": caught_msg,
                    "committed_unexpectedly": committed_unexpectedly,
                    "abox_unchanged": abox_unchanged,
                    "audit_status": audit_status,
                }
                if not passed:
                    msg = (
                        "OOB_TAMPER_UNDETECTED: external out-of-band mutation did not produce clean "
                        f"{DEVICE_STATE_MISMATCH} rejection with zero ABox write ({oob_result})"
                    )
                    _add_error(errors, error_codes, msg, code="OOB_TAMPER_UNDETECTED")
                    ok = False
                    verdict = "FALSIFIED"
            finally:
                coupled_rt.close()

    return {
        **ver_res,
        "ok": ok,
        "command": "hidden_verify",
        "verdict": verdict,
        "errors": errors,
        "error_codes": error_codes,
        "ast_errors": [],
        "oob_tamper_result": oob_result,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m open_ontology.design",
        description="OpenOntology Design-Time compiler check & Popperian verification CLI (Pure M2)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd_name in ("check", "verify"):
        p = sub.add_parser(cmd_name)
        p.add_argument(
            "--blueprint",
            action="append",
            required=True,
            help="Blueprint JSON path or 'module:symbol' (repeatable; multiple blueprints compose)",
        )
        p.add_argument("--model-id", default=None, help="Override composed model_id")
        p.add_argument("--overlay", default=None, help="Optional CustomerOverlaySpec JSON path")
        p.add_argument("--seed", default=None, help="Optional SeedOntology 'module:SYMBOL'")
        p.add_argument(
            "--device",
            action="append",
            default=[],
            help="Device descriptor JSON path or inline JSON string (repeatable)",
        )
        p.add_argument(
            "--resolution-spec",
            default=None,
            help="Optional EntityResolutionSpec JSON path",
        )
        p.add_argument(
            "--decisions",
            default=None,
            help="Optional EntityResolutionDecision list JSON path",
        )
        p.add_argument(
            "--forbidden-literal",
            action="append",
            default=[],
            help="Forbidden sample literal checked across thin Python drivers (repeatable)",
        )
        p.add_argument(
            "--max-driver-lines",
            type=int,
            default=None,
            help="Override max line budget for external thin Python drivers",
        )
        if cmd_name == "verify":
            p.add_argument("--min-action-coverage", type=float, default=0.8)
            p.add_argument("--min-transition-coverage", type=float, default=0.8)
            p.add_argument("--max-waves", type=int, default=3)
            p.add_argument(
                "--target-action",
                action="append",
                default=None,
                help="Optional explicit target action filter for falsifier (repeatable)",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "check":
        res = run_design_check(
            blueprints=args.blueprint,
            model_id=args.model_id,
            overlay=args.overlay,
            seed=args.seed,
            devices=args.device,
            resolution_spec=args.resolution_spec,
            decisions=args.decisions,
            forbidden_literals=args.forbidden_literal,
            max_driver_lines=args.max_driver_lines,
        )
    else:
        res = run_design_verify(
            blueprints=args.blueprint,
            model_id=args.model_id,
            overlay=args.overlay,
            seed=args.seed,
            devices=args.device,
            resolution_spec=args.resolution_spec,
            decisions=args.decisions,
            min_action_coverage=args.min_action_coverage,
            min_transition_coverage=args.min_transition_coverage,
            max_waves=args.max_waves,
            target_actions=args.target_action,
            forbidden_literals=args.forbidden_literal,
            max_driver_lines=args.max_driver_lines,
        )

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
