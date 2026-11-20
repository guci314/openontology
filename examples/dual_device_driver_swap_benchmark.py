"""Phase B (WO-07) 核心实证：固定标准接口热插拔双异构外设（BottleCRM + 异构 CRM）。

证明三大核心结构不变量：
1. **标准业务骨架 + 客户差量 Overlay 零改动锁定**：始终锁定 ``std_crm_ontology`` 最小核心与同一份
   ``CustomerOverlaySpec``（``CrmAccount.vip_tier(owned=True)``、开放 HIGH/URGENT 工单阻断赢单、
   ``amount > 100_000`` 输单触发 ``APPROVAL_REQUIRED_HIGH_VALUE_LOSS`` 审批流）。
2. **固定标准 ABI 驱动反编译（``DriverDecompilerAgent``）**：不修改任何标准类名与动作名，
   仅针对两套物理 Schema/主键/枚举字面量完全不同的异构 SQLite 外设自动反编译生成 ``AdapterMappingSpec``：
   - **Device A**：真实 ``vendor_crm/bottlecrm.sqlite3`` 临时副本（118+ 表、32 位 UUID 主键、``accounts/opportunity/case``、
     ``New/In Progress``、``High/Medium/Urgent``；源文件 SHA-256 严格零改动）；
   - **Device B**：动态构建的高噪异构 CRM SQLite（29 张表含 26 张噪声表、大量垃圾噪声列、整数自增主键
     ``101/201/301``、完全不同表名列名 ``corp_clients/sales_pipeline/helpdesk_issues``、完全不同枚举字面量
     ``phase_negotiate/phase_won/phase_lost`` · ``sev_p1/sev_p0/sev_p3`` · ``st_open/st_wip/st_resolved``）。
3. **同一套通用驱动桥（``GenericSQLiteDriverBridge``）与 0 行分支改动的上层业务执行器**：
   上层标准 CRM 执行函数只认识 ``CrmAccount/CrmDeal/CrmTicket/WorkCard`` 与标准动作名，分别挂载
   Device A 驱动与 Device B 驱动后，跑通完全一致的 4 项物理 SQLite 同态核销场景。

Run::
    python3 -m examples.dual_device_driver_swap_benchmark --offline
    python3 -m examples.dual_device_driver_swap_benchmark --model openrouter/openai/gpt-4.1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_ontology import (  # noqa: E402
    Actor,
    InvariantError,
    OntologyRuntime,
    OntologySpec,
    SourceBridge,
)
from open_ontology.induction import (  # noqa: E402
    AbductionBackend,
    ActionExecutionBinding,
    AdapterMappingSpec,
    AntigravityCLIAbductionBackend,
    CallableAbductionBackend,
    CrucibleContext,
    CustomerOverlaySpec,
    DecompilationCycleResult,
    DeclarativeBlueprint,
    DriverDecompilerAgent,
    EntityMappingSpec,
    ExternalEntityProfile,
    ExternalFieldProfile,
    ExternalPhenomenonProfile,
    FieldSpec,
    FieldTransformSpec,
    GeminiAbductionBackend,
    InductionGovernanceService,
    InvariantRuleSpec,
    OpenRouterAbductionBackend,
    ProposedEvalCase,
    ReplayEvent,
    STEWARD_ACTOR,
    SymbolicCrucible,
    apply_overlay,
    blueprint_fingerprint,
    compile_blueprint,
    project_external_phenomenon,
    project_single_record,
)
from examples.business_ontology import (  # noqa: E402
    BUSINESS_SEED_ONTOLOGY,
    WorkCard,
    settle_work_card,
    submit_or_open_work_card,
)
from examples.std_crm_ontology import build_std_crm_blueprint  # noqa: E402

BOTTLECRM_SQLITE_PATH = ROOT / "vendor_crm" / "bottlecrm.sqlite3"
CRM_SALES_ACTOR = Actor(actor_id="crm_sales_1", role="sales_rep")
CRM_SUPPORT_ACTOR = Actor(actor_id="crm_support_1", role="support_agent")
CRM_STEWARD_ACTOR = Actor(actor_id="crm_steward_1", role="steward")

_CATEGORICAL_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]{0,24}$")
_CHECK_IN_RE = re.compile(
    r'CHECK\s*\(\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s+IN\s*\(([^)]+)\)\s*\)',
    re.IGNORECASE,
)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1. Fixed Standard CRM ABI + Monotonic Customer Overlay
# ---------------------------------------------------------------------------
def build_standard_crm_customer_overlay() -> CustomerOverlaySpec:
    """Build the unmodified customer-specific overlay atop ``std_crm_ontology``."""
    return CustomerOverlaySpec(
        overlay_id="cust_crm_risk_and_vip_overlay",
        base_model_id="std_crm_ontology",
        target_version="1.1.0",
        description="Customer overlay: owned CrmAccount.vip_tier, critical ticket blocker on win, steward gate for >100k loss.",
        add_fields={
            "CrmAccount": [
                FieldSpec(
                    name="vip_tier",
                    field_type="str",
                    default="STANDARD",
                    owned=True,
                    description="客户自有 VIP 等级字段（不依赖外部 CRM）",
                )
            ]
        },
        add_action_preconditions={
            "win_crm_deal": [
                InvariantRuleSpec(
                    name="no_open_critical_ticket_blocks_win",
                    code="OPEN_CRITICAL_TICKET_BLOCKS_WIN",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {"op": "eq", "left": {"ref": "target.account"}, "right": {"ref": "obj.account"}},
                                {"op": "in", "left": {"ref": "target.priority"}, "right": {"lit": ["HIGH", "URGENT"]}},
                                {"op": "in", "left": {"ref": "target.status"}, "right": {"lit": ["OPEN", "IN_PROGRESS"]}},
                            ],
                        },
                    },
                    description="同一客户存在未闭环 HIGH/URGENT 级服务工单时禁止商机赢单。",
                )
            ],
            "lose_crm_deal": [
                InvariantRuleSpec(
                    name="high_value_deal_loss_requires_steward",
                    code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
                    target_type="CrmDeal",
                    id_param="deal_id",
                    when={"op": "gt", "left": {"ref": "obj.amount"}, "right": {"lit": 100000}},
                    must_hold={
                        "op": "in",
                        "left": {"ref": "actor.role"},
                        "right": {"lit": ["steward", "admin"]},
                    },
                    description="金额大于 100000 的商机输单须经 steward/admin 审批放行。",
                )
            ],
        },
    )


def build_locked_crm_target_blueprint() -> DeclarativeBlueprint:
    """Return locked ``std_crm_ontology + CustomerOverlaySpec`` blueprint (same ABI for both devices)."""
    return apply_overlay(build_std_crm_blueprint(), build_standard_crm_customer_overlay())


# ---------------------------------------------------------------------------
# 2. Unsupervised SQLite Topological Scout + Device B Constructor
# ---------------------------------------------------------------------------
def build_device_b_heterogeneous_sqlite(db_path: Path) -> dict[str, Any]:
    """Construct temporary Device B: 29 SQLite tables (26 noise), integer PKs, proprietary names & enums."""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # 26 garbage noise tables (isolated or unlinked config/log/queue noise tables)
        for i in range(1, 27):
            tname = f"noise_legacy_artifact_{i:02d}"
            conn.execute(
                f'CREATE TABLE "{tname}" ("nid" INTEGER PRIMARY KEY, "payload" TEXT, "flag" INTEGER)'
            )
            for j in range(1, 4):
                conn.execute(
                    f'INSERT INTO "{tname}" ("nid", "payload", "flag") VALUES (?, ?, ?)',
                    (j, f"junk-{i}-{j}", j % 2),
                )

        conn.execute(
            """
            CREATE TABLE "corp_clients" (
                "client_pk" INTEGER PRIMARY KEY,
                "corp_title" TEXT NOT NULL,
                "sector_code" TEXT NOT NULL,
                "active_flag" INTEGER NOT NULL DEFAULT 1,
                "legacy_erp_blob" TEXT DEFAULT '{}',
                "ui_color_hex" TEXT DEFAULT '#112233',
                "sync_epoch_ms" INTEGER DEFAULT 0,
                "shard_hint" TEXT DEFAULT 'sh-0',
                "internal_tag_csv" TEXT DEFAULT '',
                "marketing_opt_raw" INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE "sales_pipeline" (
                "deal_pk" INTEGER PRIMARY KEY,
                "client_fk" INTEGER NOT NULL REFERENCES "corp_clients" ("client_pk"),
                "deal_headline" TEXT NOT NULL,
                "deal_value_usd" REAL NOT NULL,
                "pipeline_phase" TEXT NOT NULL CHECK (
                    "pipeline_phase" IN ('phase_qual', 'phase_proposal', 'phase_negotiate', 'phase_won', 'phase_lost')
                ),
                "loss_memo" TEXT NOT NULL DEFAULT '',
                "forecast_bucket" TEXT DEFAULT 'Q4',
                "kanban_pos" INTEGER DEFAULT 1,
                "owner_avatar_url" TEXT DEFAULT '',
                "margin_estimate_buf" REAL DEFAULT 0.2,
                "raw_webhook_payload" TEXT DEFAULT '{}',
                "created_epoch" INTEGER DEFAULT 1700000000
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE "helpdesk_issues" (
                "issue_pk" INTEGER PRIMARY KEY,
                "client_fk" INTEGER NOT NULL REFERENCES "corp_clients" ("client_pk"),
                "issue_title" TEXT NOT NULL,
                "issue_severity" TEXT NOT NULL CHECK (
                    "issue_severity" IN ('sev_p0', 'sev_p1', 'sev_p3', 'sev_p4')
                ),
                "issue_state" TEXT NOT NULL CHECK (
                    "issue_state" IN ('st_open', 'st_wip', 'st_resolved', 'st_closed')
                ),
                "closing_note" TEXT NOT NULL DEFAULT '',
                "sla_tick_counter" INTEGER DEFAULT 0,
                "queue_partition" TEXT DEFAULT 'default',
                "escalation_hops" INTEGER DEFAULT 0,
                "channel_meta" TEXT DEFAULT 'email',
                "archived_flag" INTEGER DEFAULT 0
            )
            """
        )

        # Clients (101, 102, 103 active; 104 archive customer holding terminal reference rows)
        conn.executemany(
            'INSERT INTO "corp_clients" ("client_pk", "corp_title", "sector_code", "active_flag") VALUES (?, ?, ?, ?)',
            [
                (101, "Alpha Precision Machinery", "INDUSTRIAL_MFG", 1),
                (102, "Borealis Retail Group", "COMMERCE_RETAIL", 1),
                (103, "Crescent Health Systems", "MEDICAL_TECH", 1),
                (104, "Historical Archive Corp", "ARCHIVE", 1),
            ],
        )
        conn.executemany(
            'INSERT INTO "sales_pipeline" ("deal_pk", "client_fk", "deal_headline", "deal_value_usd", "pipeline_phase") VALUES (?, ?, ?, ?, ?)',
            [
                (201, 101, "Alpha Smart Line Phase 1", 48_000.0, "phase_negotiate"),
                (202, 102, "Borealis Annual Supply Agreement", 12_000.0, "phase_proposal"),
                (203, 103, "Crescent MRI Suite Upgrade", 260_000.0, "phase_qual"),
                (204, 104, "Archive Historical Won Deal", 15_000.0, "phase_won"),
                (205, 104, "Archive Historical Lost Deal", 9_000.0, "phase_lost"),
            ],
        )
        conn.executemany(
            'INSERT INTO "helpdesk_issues" ("issue_pk", "client_fk", "issue_title", "issue_severity", "issue_state") VALUES (?, ?, ?, ?, ?)',
            [
                (301, 101, "Packaging Seal Sensor Fault", "sev_p1", "st_open"),
                (302, 102, "Invoice PDF Copy Mismatch", "sev_p3", "st_wip"),
                (303, 103, "Cold Chain Dock Delay Alert", "sev_p0", "st_open"),
                (304, 104, "Archive Calibrated Sensor", "sev_p4", "st_resolved"),
                (305, 104, "Archive Closed Inquiry", "sev_p3", "st_closed"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"db_path": db_path, "total_tables_expected": 29}


def scout_sqlite_device(db_path: Path, *, source_kind: str = "sqlite_device") -> dict[str, Any]:
    """Unsupervised, zero-hardcoding SQLite metadata, DDL CHECK, and FK subgraph scout."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        all_tables = [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        domain_tables = [
            t for t in all_tables if not t.startswith("django_") and not t.startswith("auth_")
        ]

        ddl_by_table: dict[str, str] = {}
        for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall():
            if row["sql"]:
                ddl_by_table[str(row["name"])] = str(row["sql"])

        cols_by_table: dict[str, list[Any]] = {}
        fks_by_table: dict[str, list[dict[str, str]]] = {}
        row_count_by_table: dict[str, int] = {}
        raw_total_cols = 0
        for tbl in domain_tables:
            cols = conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()
            cols_by_table[tbl] = cols
            raw_total_cols += len(cols)
            fks = conn.execute(f'PRAGMA foreign_key_list("{tbl}")').fetchall()
            fks_by_table[tbl] = [
                {"from_table": tbl, "from_col": str(fk["from"]), "to_table": str(fk["table"])}
                for fk in fks
            ]
            row_count_by_table[tbl] = int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0])

        # Core subgraph: populated multi-row tables connected by non-self foreign keys
        multi_row_tables = {t for t in domain_tables if row_count_by_table[t] >= 2}
        multi_degree: dict[str, int] = {t: 0 for t in multi_row_tables}
        for tbl in multi_row_tables:
            for fk in fks_by_table[tbl]:
                tgt = fk["to_table"]
                if tgt in multi_row_tables and tgt != tbl:
                    multi_degree[tbl] += 1
                    multi_degree[tgt] += 1
        core_tables = sorted(t for t, deg in multi_degree.items() if deg >= 1)
        core_set = set(core_tables)
        core_fk_targets = {
            fk["to_table"] for tbl in core_tables for fk in fks_by_table[tbl] if fk["to_table"] != tbl
        }

        entities: list[ExternalEntityProfile] = []
        raw_rows_by_table: dict[str, list[dict[str, Any]]] = {}
        table_pks: dict[str, str] = {}
        fk_edges: list[dict[str, str]] = []

        for tbl in core_tables:
            cols = cols_by_table[tbl]
            pk_cols = [str(c["name"]) for c in cols if int(c["pk"] or 0) > 0]
            if pk_cols:
                table_pks[tbl] = pk_cols[0]
            ddl = ddl_by_table.get(tbl, "")
            check_enums: dict[str, list[str]] = {}
            for col_match, vals_blob in _CHECK_IN_RE.findall(ddl):
                lits = [v.strip().strip("'\"") for v in vals_blob.split(",") if v.strip()]
                if lits:
                    check_enums[col_match] = lits

            fk_by_col = {fk["from_col"]: fk["to_table"] for fk in fks_by_table[tbl]}
            for fk in fks_by_table[tbl]:
                if fk["to_table"] in core_set:
                    fk_edges.append(dict(fk))
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{tbl}" ORDER BY rowid').fetchall()]
            raw_rows_by_table[tbl] = rows

            fields: list[ExternalFieldProfile] = []
            for c in cols:
                cname = str(c["name"])
                ctype = str(c["type"] or "varchar").lower()
                is_pk = int(c["pk"] or 0) > 0
                if "int" in ctype or "bool" in ctype:
                    rtype = "integer"
                elif "real" in ctype or "dec" in ctype or "float" in ctype:
                    rtype = "float"
                else:
                    rtype = "string"

                enums: list[str] = list(check_enums.get(cname, []))
                if rtype == "string" and not is_pk and cname not in fk_by_col:
                    vals = [
                        str(r[cname])
                        for r in rows
                        if r.get(cname) is not None and str(r.get(cname)).strip() != ""
                    ]
                    distinct = sorted(set(vals))
                    if (
                        1 <= len(distinct) <= 10
                        and all(_CATEGORICAL_TOKEN_RE.match(v) for v in distinct)
                        and (len(distinct) < len(rows) or all(" " not in v for v in distinct))
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
                    row_count=len(rows),
                    fields=fields,
                    sample_rows=rows,
                )
            )

        discovered_lookup_codes: dict[str, list[str]] = {}
        for tbl in sorted(multi_row_tables - core_set):
            col_names = {str(c["name"]) for c in cols_by_table[tbl]}
            tbl_fk_targets = {fk["to_table"] for fk in fks_by_table[tbl]}
            if "code" in col_names and (tbl_fk_targets & core_fk_targets):
                order_col = '"order"' if "order" in col_names else "rowid"
                codes = [
                    str(r[0])
                    for r in conn.execute(f'SELECT code FROM "{tbl}" ORDER BY {order_col}').fetchall()
                    if r[0] is not None and str(r[0]).strip() != ""
                ]
                if codes:
                    discovered_lookup_codes[tbl] = codes

        profile = ExternalPhenomenonProfile(
            source_kind=source_kind,
            source_locator=f"sqlite://{db_path.name}",
            entities=entities,
        )
        return {
            "total_sqlite_tables": len(all_tables),
            "domain_table_count": len(domain_tables),
            "raw_domain_columns_total": raw_total_cols,
            "scouted_table_columns": sum(len(e.fields) for e in entities),
            "profile": profile,
            "raw_rows_by_table": raw_rows_by_table,
            "table_pks": table_pks,
            "fk_edges": fk_edges,
            "discovered_lookup_codes": discovered_lookup_codes,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Generic Bidirectional SQLite Driver Bridge (Zero Device Hardcoding)
# ---------------------------------------------------------------------------
class GenericSQLiteDriverBridge:
    """Generic declarative SQLite SourceBridge driven solely by ``AdapterMappingSpec`` + PRAGMA."""

    def __init__(
        self,
        db_path: Path,
        *,
        spec: OntologySpec,
        profile: ExternalPhenomenonProfile,
        adapter_mapping: AdapterMappingSpec,
    ) -> None:
        self.db_path = db_path
        self.spec = spec
        self.profile = profile
        self.adapter_mapping = adapter_mapping
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._em_by_type: dict[str, EntityMappingSpec] = {
            em.target_object_type: em for em in adapter_mapping.entity_mappings
        }
        self._col_meta: dict[str, dict[str, dict[str, Any]]] = {}
        self._pk_col: dict[str, str] = {}
        for em in adapter_mapping.entity_mappings:
            tbl = em.source_table
            if tbl in self._col_meta:
                continue
            rows = self.conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()
            self._col_meta[tbl] = {
                str(r["name"]): {"type": str(r["type"] or "").upper(), "pk": int(r["pk"] or 0)}
                for r in rows
            }
            pk_cols = [str(r["name"]) for r in rows if int(r["pk"] or 0) > 0]
            self._pk_col[tbl] = pk_cols[0] if pk_cols else (em.id_transform.source_field or "id")
        self._lookup_index = self._build_initial_lookup_index()

    def close(self) -> None:
        self.conn.close()

    def _build_initial_lookup_index(self) -> dict[tuple[str, str, str], str]:
        lookup: dict[tuple[str, str, str], str] = {}
        entities_by_table = {e.table_name: e for e in self.profile.entities}
        for em in self.adapter_mapping.entity_mappings:
            src_ent = entities_by_table.get(em.source_table)
            if src_ent is None:
                continue
            prefix = em.id_transform.prefix or ""
            src_pk = em.id_transform.source_field or self._pk_col.get(em.source_table, "id")
            for row in src_ent.sample_rows:
                raw_id = row.get(src_pk)
                if raw_id is None:
                    continue
                cid = f"{prefix}{raw_id}"
                for k, v in row.items():
                    if v is not None and not isinstance(v, (list, dict)):
                        lookup[(em.source_table, str(k), str(v))] = cid
        return lookup

    def _raw_pk(self, em: EntityMappingSpec, canonical_id: str) -> Any:
        prefix = em.id_transform.prefix or ""
        raw = canonical_id[len(prefix) :] if prefix and canonical_id.startswith(prefix) else canonical_id
        pk_col = self._pk_col.get(em.source_table, em.id_transform.source_field or "id")
        pk_type = self._col_meta.get(em.source_table, {}).get(pk_col, {}).get("type", "")
        if "INT" in pk_type and raw.lstrip("-").isdigit():
            return int(raw)
        return raw

    @staticmethod
    def _encode_field_value(ft: FieldTransformSpec, value: Any) -> Any:
        if ft.kind == "enum_map":
            inv = {v: k for k, v in (ft.enum_map or {}).items()}
            return inv.get(str(value), value)
        if ft.kind in ("id_prefix", "foreign_key_id") and ft.prefix:
            sval = str(value)
            return sval[len(ft.prefix) :] if sval.startswith(ft.prefix) else sval
        if ft.kind == "scale_int" and ft.scale:
            return float(value) / ft.scale if value is not None else value
        if isinstance(value, bool):
            return int(value)
        return value

    def actuate(self, move_envelope: dict[str, Any]) -> None:
        diff_changes = (move_envelope.get("object_diff") or {}).get("object_changes") or []
        for ch in diff_changes:
            tname = str(ch.get("object_type") or "")
            oid = str(ch.get("object_id") or "")
            cls = self.spec.get_object_type(tname)
            if cls.is_fully_owned():
                continue
            em = self._em_by_type.get(tname)
            if em is None:
                continue
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
                if ft is None or not ft.source_field or ft.source_field not in valid_cols:
                    continue
                updates[ft.source_field] = self._encode_field_value(ft, after_val)
            if updates:
                pk_col = self._pk_col[em.source_table]
                raw_pk = self._raw_pk(em, oid)
                set_sql = ", ".join(f'"{col}"=?' for col in updates)
                self.conn.execute(
                    f'UPDATE "{em.source_table}" SET {set_sql} WHERE "{pk_col}"=?',
                    (*updates.values(), raw_pk),
                )
        self.conn.commit()

    def observe(self, move_envelope: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        diff_changes = (move_envelope.get("object_diff") or {}).get("object_changes") or []
        observed: dict[str, list[dict[str, Any]]] = {}
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
            em = self._em_by_type.get(tname)
            if em is None:
                continue
            pk_col = self._pk_col[em.source_table]
            raw_pk = self._raw_pk(em, oid)
            row = self.conn.execute(
                f'SELECT * FROM "{em.source_table}" WHERE "{pk_col}"=?',
                (raw_pk,),
            ).fetchone()
            if row is None:
                continue
            projected = project_single_record(
                em,
                dict(row),
                lookup_index=self._lookup_index,
                treat_false_as_null=self.adapter_mapping.treat_false_as_null,
            )
            observed.setdefault(tname, []).append(projected)
        return observed

    def as_source_bridge(self) -> SourceBridge:
        return SourceBridge(actuate=self.actuate, observe=self.observe)

    def fetch_raw_row(self, object_type: str, canonical_id: str) -> dict[str, Any]:
        em = self._em_by_type[object_type]
        pk_col = self._pk_col[em.source_table]
        raw_pk = self._raw_pk(em, canonical_id)
        row = self.conn.execute(
            f'SELECT * FROM "{em.source_table}" WHERE "{pk_col}"=?',
            (raw_pk,),
        ).fetchone()
        return dict(row) if row is not None else {}


# ---------------------------------------------------------------------------
# 4. Device-Agnostic Replay Factory + Reference Offline Decompiler Backends
# ---------------------------------------------------------------------------
def build_crm_decompiler_replay_factory() -> Callable[
    [list[dict[str, Any]], DeclarativeBlueprint, AdapterMappingSpec | None],
    tuple[list[ReplayEvent], list[ReplayEvent]],
]:
    """Verify M0 projection semantics across win/lose/resolve regardless of source device."""

    def _factory(
        migrated: list[dict[str, Any]],
        candidate: DeclarativeBlueprint,
        adapter_mapping: AdapterMappingSpec | None,
    ) -> tuple[list[ReplayEvent], list[ReplayEvent]]:
        _ = candidate, adapter_mapping
        pos: list[ReplayEvent] = []
        neg: list[ReplayEvent] = []
        deals = [e["data"] for e in migrated if e.get("object_type") == "CrmDeal"]
        tickets = [e["data"] for e in migrated if e.get("object_type") == "CrmTicket"]
        active_stages = {"PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION"}
        winnable_stages = {"PROPOSAL", "NEGOTIATION"}
        critical_accounts = {
            str(t.get("account"))
            for t in tickets
            if str(t.get("priority")) in ("HIGH", "URGENT")
            and str(t.get("status")) in ("OPEN", "IN_PROGRESS")
        }

        crit_ticket = next(
            (
                t
                for t in tickets
                if str(t.get("priority")) in ("HIGH", "URGENT")
                and str(t.get("status")) in ("OPEN", "IN_PROGRESS")
            ),
            None,
        )
        if crit_ticket is not None:
            pos.append(
                ReplayEvent(
                    name="DEC-POS-RESOLVE-CRIT-TICKET",
                    action="resolve_crm_ticket",
                    params={"ticket_id": str(crit_ticket["ticket_id"])},
                    actor=CRM_SUPPORT_ACTOR,
                    expected_valid=True,
                )
            )

        blocked_deal = next(
            (
                d
                for d in deals
                if str(d.get("account")) in critical_accounts
                and str(d.get("stage")) in winnable_stages
                and float(d.get("amount") or 0.0) <= 100_000.0
            ),
            None,
        )
        if blocked_deal is not None:
            neg.append(
                ReplayEvent(
                    name="DEC-NEG-BLOCKED-WIN-CRIT-TICKET",
                    action="win_crm_deal",
                    params={"deal_id": str(blocked_deal["deal_id"])},
                    actor=CRM_SALES_ACTOR,
                    expected_valid=False,
                    expected_error_code="OPEN_CRITICAL_TICKET_BLOCKS_WIN",
                )
            )

        clean_win_deal = next(
            (
                d
                for d in deals
                if str(d.get("account")) not in critical_accounts
                and str(d.get("stage")) in winnable_stages
            ),
            None,
        )
        if clean_win_deal is not None:
            pos.append(
                ReplayEvent(
                    name="DEC-POS-WIN-MEDIUM-TICKET",
                    action="win_crm_deal",
                    params={"deal_id": str(clean_win_deal["deal_id"])},
                    actor=CRM_SALES_ACTOR,
                    expected_valid=True,
                )
            )

        big_deal = next(
            (
                d
                for d in deals
                if str(d.get("stage")) in active_stages and float(d.get("amount") or 0.0) > 100_000.0
            ),
            None,
        )
        if big_deal is not None:
            neg.append(
                ReplayEvent(
                    name="DEC-NEG-HIGH-VALUE-LOSS-SALES",
                    action="lose_crm_deal",
                    params={"deal_id": str(big_deal["deal_id"])},
                    actor=CRM_SALES_ACTOR,
                    expected_valid=False,
                    expected_error_code="APPROVAL_REQUIRED_HIGH_VALUE_LOSS",
                )
            )
            pos.append(
                ReplayEvent(
                    name="DEC-POS-HIGH-VALUE-LOSS-STEWARD",
                    action="lose_crm_deal",
                    params={"deal_id": str(big_deal["deal_id"])},
                    actor=CRM_STEWARD_ACTOR,
                    expected_valid=True,
                )
            )

        return pos, neg

    return _factory


def build_offline_device_a_mapping() -> AdapterMappingSpec:
    """Reference offline ``AdapterMappingSpec`` aligning BottleCRM SQLite -> ``std_crm`` ABI."""
    return AdapterMappingSpec(
        mapping_id="driver_device_a_bottlecrm",
        source_kind="sqlite_bottlecrm",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="id", kind="id_prefix", prefix="PTY-"),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct", default="CN"),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="CrmAccount",
                id_transform=FieldTransformSpec(target_field="account_id", source_field="id", kind="id_prefix", prefix="ACC-"),
                field_transforms=[
                    FieldTransformSpec(target_field="party", source_field="id", kind="foreign_key_id", prefix="PTY-"),
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="industry", source_field="industry", kind="direct", default=""),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="opportunity",
                target_object_type="CrmDeal",
                id_transform=FieldTransformSpec(target_field="deal_id", source_field="id", kind="id_prefix", prefix="DEAL-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account", source_field="account_id", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="title", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="amount", source_field="amount", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="stage", source_field="stage", kind="direct", default="PROSPECTING"),
                ],
            ),
            EntityMappingSpec(
                source_table="case",
                target_object_type="CrmTicket",
                id_transform=FieldTransformSpec(target_field="ticket_id", source_field="id", kind="id_prefix", prefix="TK-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account", source_field="account_id", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="subject", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="priority",
                        source_field="priority",
                        kind="enum_map",
                        default="MEDIUM",
                        enum_map={
                            "Urgent": "URGENT",
                            "High": "HIGH",
                            "Medium": "MEDIUM",
                            "Low": "LOW",
                        },
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="status",
                        kind="enum_map",
                        default="OPEN",
                        enum_map={
                            "New": "OPEN",
                            "Assigned": "OPEN",
                            "In Progress": "IN_PROGRESS",
                            "Resolved": "RESOLVED",
                            "Closed": "CLOSED",
                        },
                    ),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="win_crm_deal",
                external_table="opportunity",
                external_method="stage=CLOSED_WON",
                target_object_type="CrmDeal",
                id_param="deal_id",
                id_prefix="DEAL-",
                expected_state_field="stage",
                expected_state_after="CLOSED_WON",
            ),
            ActionExecutionBinding(
                action_name="lose_crm_deal",
                external_table="opportunity",
                external_method="stage=CLOSED_LOST",
                target_object_type="CrmDeal",
                id_param="deal_id",
                id_prefix="DEAL-",
                expected_state_field="stage",
                expected_state_after="CLOSED_LOST",
            ),
            ActionExecutionBinding(
                action_name="resolve_crm_ticket",
                external_table="case",
                external_method="status=Resolved",
                target_object_type="CrmTicket",
                id_param="ticket_id",
                id_prefix="TK-",
                expected_state_field="status",
                expected_state_after="RESOLVED",
            ),
        ],
    )


def build_offline_device_b_mapping() -> AdapterMappingSpec:
    """Reference offline ``AdapterMappingSpec`` aligning heterogeneous Device B SQLite -> ``std_crm`` ABI."""
    return AdapterMappingSpec(
        mapping_id="driver_device_b_heterogeneous",
        source_kind="sqlite_heterogeneous_crm",
        treat_false_as_null=False,
        entity_mappings=[
            EntityMappingSpec(
                source_table="corp_clients",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(target_field="party_id", source_field="client_pk", kind="id_prefix", prefix="PTY-"),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="corp_title", kind="direct", default=""),
                    FieldTransformSpec(target_field="status", source_field="active_flag", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="corp_clients",
                target_object_type="CrmAccount",
                id_transform=FieldTransformSpec(target_field="account_id", source_field="client_pk", kind="id_prefix", prefix="ACC-"),
                field_transforms=[
                    FieldTransformSpec(target_field="party", source_field="client_pk", kind="foreign_key_id", prefix="PTY-"),
                    FieldTransformSpec(target_field="name", source_field="corp_title", kind="direct", default=""),
                    FieldTransformSpec(target_field="industry", source_field="sector_code", kind="direct", default=""),
                    FieldTransformSpec(target_field="status", source_field="active_flag", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="sales_pipeline",
                target_object_type="CrmDeal",
                id_transform=FieldTransformSpec(target_field="deal_id", source_field="deal_pk", kind="id_prefix", prefix="DEAL-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account", source_field="client_fk", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="title", source_field="deal_headline", kind="direct", default=""),
                    FieldTransformSpec(target_field="amount", source_field="deal_value_usd", kind="direct", default=0.0),
                    FieldTransformSpec(
                        target_field="stage",
                        source_field="pipeline_phase",
                        kind="enum_map",
                        default="PROSPECTING",
                        enum_map={
                            "phase_qual": "QUALIFICATION",
                            "phase_proposal": "PROPOSAL",
                            "phase_negotiate": "NEGOTIATION",
                            "phase_won": "CLOSED_WON",
                            "phase_lost": "CLOSED_LOST",
                        },
                    ),
                    FieldTransformSpec(target_field="loss_reason", source_field="loss_memo", kind="direct", default=""),
                ],
            ),
            EntityMappingSpec(
                source_table="helpdesk_issues",
                target_object_type="CrmTicket",
                id_transform=FieldTransformSpec(target_field="ticket_id", source_field="issue_pk", kind="id_prefix", prefix="TK-"),
                field_transforms=[
                    FieldTransformSpec(target_field="account", source_field="client_fk", kind="foreign_key_id", prefix="ACC-"),
                    FieldTransformSpec(target_field="subject", source_field="issue_title", kind="direct", default=""),
                    FieldTransformSpec(
                        target_field="priority",
                        source_field="issue_severity",
                        kind="enum_map",
                        default="MEDIUM",
                        enum_map={
                            "sev_p0": "URGENT",
                            "sev_p1": "HIGH",
                            "sev_p3": "MEDIUM",
                            "sev_p4": "LOW",
                        },
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="issue_state",
                        kind="enum_map",
                        default="OPEN",
                        enum_map={
                            "st_open": "OPEN",
                            "st_wip": "IN_PROGRESS",
                            "st_resolved": "RESOLVED",
                            "st_closed": "CLOSED",
                        },
                    ),
                    FieldTransformSpec(target_field="resolution_summary", source_field="closing_note", kind="direct", default=""),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="win_crm_deal",
                external_table="sales_pipeline",
                external_method="pipeline_phase=phase_won",
                target_object_type="CrmDeal",
                id_param="deal_id",
                id_prefix="DEAL-",
                expected_state_field="stage",
                expected_state_after="CLOSED_WON",
            ),
            ActionExecutionBinding(
                action_name="lose_crm_deal",
                external_table="sales_pipeline",
                external_method="pipeline_phase=phase_lost",
                target_object_type="CrmDeal",
                id_param="deal_id",
                id_prefix="DEAL-",
                expected_state_field="stage",
                expected_state_after="CLOSED_LOST",
            ),
            ActionExecutionBinding(
                action_name="resolve_crm_ticket",
                external_table="helpdesk_issues",
                external_method="issue_state=st_resolved",
                target_object_type="CrmTicket",
                id_param="ticket_id",
                id_prefix="TK-",
                expected_state_field="status",
                expected_state_after="RESOLVED",
            ),
        ],
    )


def _offline_backend_for_mapping(mapping: AdapterMappingSpec) -> CallableAbductionBackend:
    sample_eval = [
        ProposedEvalCase(
            name="DECOMP-EVAL-WIN-DEAL",
            actor={"actor_id": "eval:sales", "role": "sales_rep"},
            action="win_crm_deal",
            params={"deal_id": "DEAL-E1"},
            setup_objects=[
                {"object_type": "BusinessParty", "data": {"party_id": "PTY-E1", "legal_name": "Eval Party"}},
                {"object_type": "CrmAccount", "data": {"account_id": "ACC-E1", "name": "Eval Acc", "party": "PTY-E1"}},
                {"object_type": "CrmDeal", "data": {"deal_id": "DEAL-E1", "title": "Eval Deal", "account": "ACC-E1", "amount": 5000.0, "stage": "NEGOTIATION"}},
            ],
            expect={"valid": True},
        ).model_dump(mode="json")
    ]

    def _fn(_s: str, _u: str, _r: int) -> dict[str, Any]:
        return {
            "rationale": f"Decompiled driver {mapping.mapping_id}",
            "adapter_mapping": mapping.model_dump(mode="json"),
            "customer_overlay": None,
            "proposed_eval_cases": sample_eval,
        }

    return CallableAbductionBackend(_fn)


# ---------------------------------------------------------------------------
# 5. Zero-Branch Standard CRM Upper Business Workload Executor
# ---------------------------------------------------------------------------
def run_standard_crm_workload(rt: OntologyRuntime) -> dict[str, Any]:
    """Execute 4 homomorphic CRM governance scenarios using ONLY canonical ``std_crm`` + Overlay API.

    Zero knowledge of underlying physical database, table names, column names, or source enums!
    """
    sales = CRM_SALES_ACTOR
    support = CRM_SUPPORT_ACTOR
    steward = CRM_STEWARD_ACTOR

    deals = rt.query_objects("CrmDeal", sales)
    tickets = rt.query_objects("CrmTicket", sales)

    active_critical_tickets = [
        t for t in tickets if t.priority in ("HIGH", "URGENT") and t.status in ("OPEN", "IN_PROGRESS")
    ]
    medium_low_accounts = {
        t.account for t in tickets if t.priority in ("MEDIUM", "LOW") and t.status in ("OPEN", "IN_PROGRESS")
    } - {t.account for t in active_critical_tickets}

    # Select canonical subjects dynamically by M1 business predicates
    critical_ticket = next(
        t
        for t in active_critical_tickets
        if any(
            d.account == t.account and d.amount <= 100_000.0 and d.stage in ("PROPOSAL", "NEGOTIATION")
            for d in deals
        )
    )
    blocked_deal = next(
        d
        for d in deals
        if d.account == critical_ticket.account and d.amount <= 100_000.0 and d.stage in ("PROPOSAL", "NEGOTIATION")
    )
    big_loss_deal = next(
        d
        for d in deals
        if d.amount > 100_000.0 and d.stage in ("PROSPECTING", "QUALIFICATION", "PROPOSAL", "NEGOTIATION")
    )
    medium_case_deal = next(
        d
        for d in deals
        if d.account in medium_low_accounts
        and d.amount <= 100_000.0
        and d.stage in ("PROPOSAL", "NEGOTIATION")
        and d.deal_id != blocked_deal.deal_id
    )

    # Check owned overlay field CrmAccount.vip_tier works seamlessly on external-backed accounts
    acc_cls = rt.spec.get_object_type("CrmAccount")
    target_acc = rt.get_object("CrmAccount", blocked_deal.account, sales)
    rt.put_object(
        acc_cls(**{**target_acc.model_dump(), "vip_tier": "PLATINUM"}),
        actor=steward,
    )
    assert rt.get_object("CrmAccount", blocked_deal.account, sales).vip_tier == "PLATINUM"

    # Scenario 1: Open HIGH/URGENT ticket blocks win_crm_deal (0 source writes)
    blocked_err_code: str | None = None
    try:
        rt.submit_action("CRM-S1-WIN-BLK", "win_crm_deal", {"deal_id": blocked_deal.deal_id}, sales)
        raise AssertionError("expected win_crm_deal to be blocked by open critical ticket")
    except InvariantError as exc:
        blocked_err_code = exc.code
    deal_after_s1 = rt.get_object("CrmDeal", blocked_deal.deal_id, sales)

    # Scenario 2: Resolve critical ticket -> retry win_crm_deal commits homomorphically
    s2_ticket_run = rt.submit_action(
        "CRM-S2-RESOLVE-TK",
        "resolve_crm_ticket",
        {"ticket_id": critical_ticket.ticket_id},
        support,
    )
    s2_win_run = rt.submit_action(
        "CRM-S2-WIN-OK",
        "win_crm_deal",
        {"deal_id": blocked_deal.deal_id},
        sales,
    )
    ticket_after_s2 = rt.get_object("CrmTicket", critical_ticket.ticket_id, support)
    deal_after_s2 = rt.get_object("CrmDeal", blocked_deal.deal_id, sales)

    # Scenario 3: >100k lose_crm_deal -> APPROVAL_REQUIRED_* -> WorkCard -> SoD -> steward settle
    s3_esc = submit_or_open_work_card(
        rt,
        "CRM-S3-LOSE-BIG",
        "lose_crm_deal",
        {"deal_id": big_loss_deal.deal_id},
        sales,
        target_object_type="CrmDeal",
        target_object_id=big_loss_deal.deal_id,
        assignee_role="steward",
        card_id=f"WC-LOSS-{big_loss_deal.deal_id}",
    )
    sod_blocked_code: str | None = None
    try:
        settle_work_card(
            rt,
            s3_esc["card_id"],
            Actor(actor_id=sales.actor_id, role="steward"),
        )
        raise AssertionError("expected SoD violation for same actor_id self-settlement")
    except InvariantError as exc:
        sod_blocked_code = exc.code

    s3_settled = settle_work_card(
        rt,
        s3_esc["card_id"],
        steward,
        resolution_note="approved high-value deal close-lost settlement",
    )
    wc_after_s3 = rt.get_object(WorkCard, s3_esc["card_id"], steward)
    deal_after_s3 = rt.get_object("CrmDeal", big_loss_deal.deal_id, steward)

    # Scenario 4: Medium-priority ticket does NOT block win_crm_deal
    s4_win_run = rt.submit_action(
        "CRM-S4-WIN-MED",
        "win_crm_deal",
        {"deal_id": medium_case_deal.deal_id},
        sales,
    )
    deal_after_s4 = rt.get_object("CrmDeal", medium_case_deal.deal_id, sales)

    return {
        "selected_ids": {
            "blocked_deal_id": blocked_deal.deal_id,
            "critical_ticket_id": critical_ticket.ticket_id,
            "big_loss_deal_id": big_loss_deal.deal_id,
            "medium_case_deal_id": medium_case_deal.deal_id,
        },
        "scenario_1_critical_ticket_blocks_win": {
            "blocked": blocked_err_code is not None,
            "error_code": blocked_err_code,
            "stage_unchanged": deal_after_s1.stage == blocked_deal.stage,
        },
        "scenario_2_resolve_then_win": {
            "ticket_run_status": s2_ticket_run["status"],
            "ticket_status": ticket_after_s2.status,
            "win_run_status": s2_win_run["status"],
            "deal_stage": deal_after_s2.stage,
        },
        "scenario_3_high_value_loss_work_card": {
            "escalation_status": s3_esc["status"],
            "escalation_error_code": s3_esc["error_code"],
            "sod_blocked_code": sod_blocked_code,
            "settled_action_status": s3_settled["settled_action_run"]["status"],
            "work_card_status": wc_after_s3.status,
            "deal_stage": deal_after_s3.stage,
        },
        "scenario_4_medium_ticket_allows_win": {
            "win_run_status": s4_win_run["status"],
            "deal_stage": deal_after_s4.stage,
        },
    }


# ---------------------------------------------------------------------------
# 6. Dual-Device Orchestration & Verification
# ---------------------------------------------------------------------------
def _make_backend(model: str) -> tuple[AbductionBackend | None, str]:
    if model == "offline":
        return None, "offline-reference"
    if model.startswith("openrouter/"):
        slug = model[len("openrouter/") :]
        return OpenRouterAbductionBackend(model=slug, timeout_sec=180), model
    if model.startswith("gemini/"):
        slug = model[len("gemini/") :]
        return GeminiAbductionBackend(model=slug, timeout_sec=180), model
    slug = model[len("agy/") :] if model.startswith("agy/") else model
    return AntigravityCLIAbductionBackend(model=slug, timeout_sec=240), model


def _decompile_and_execute_on_device(
    *,
    device_label: str,
    model_id: str,
    db_path: Path,
    locked_blueprint: DeclarativeBlueprint,
    effective_spec: OntologySpec,
    backend_override: AbductionBackend,
    max_rounds: int,
) -> dict[str, Any]:
    scout = scout_sqlite_device(db_path, source_kind=f"sqlite_{device_label}")
    profile: ExternalPhenomenonProfile = scout["profile"]
    t0 = time.monotonic()

    with InductionGovernanceService(db_path=":memory:") as gov:
        decompiler = DriverDecompilerAgent(
            backend=backend_override,
            governance=gov,
            crucible=SymbolicCrucible(),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
        )
        dec_res: DecompilationCycleResult = decompiler.run_decompilation_cycle(
            model_id=model_id,
            target_blueprint=locked_blueprint,
            external_profile=profile,
            phenomenon_brief={
                "discovered_entities": [e.table_name for e in profile.entities],
                "discovered_primary_keys": scout["table_pks"],
                "discovered_fk_edges": scout["fk_edges"],
                "discovered_lookup_codes": scout["discovered_lookup_codes"],
            },
            required_object_types=("BusinessParty", "CrmAccount", "CrmDeal", "CrmTicket"),
            required_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
            crucible_context=CrucibleContext(
                mode="genesis",
                replay_factory=build_crm_decompiler_replay_factory(),
                default_actor=CRM_SALES_ACTOR,
                seed_ontology=BUSINESS_SEED_ONTOLOGY,
            ),
            seed_ontology=BUSINESS_SEED_ONTOLOGY,
            max_rounds=max_rounds,
        )
        elapsed_sec = round(time.monotonic() - t0, 2)
        if not dec_res.verified or dec_res.pending_approval_run_id is None:
            raise RuntimeError(
                f"{device_label} decompilation failed:\n{dec_res.final_report.diagnostic_feedback()}"
            )
        gov.approve_crystallization(dec_res.pending_approval_run_id, STEWARD_ACTOR)
        mapping = gov.get_active_adapter_mapping(model_id)
        eff_bp = gov.get_active_blueprint(model_id)
        assert mapping is not None and eff_bp is not None

    projected, proj_errors = project_external_phenomenon(profile, mapping)
    assert not proj_errors, proj_errors

    bridge = GenericSQLiteDriverBridge(
        db_path,
        spec=effective_spec,
        profile=profile,
        adapter_mapping=mapping,
    )
    rt = OntologyRuntime(
        effective_spec,
        db_path=":memory:",
        world_mode="coupled",
        source_bridge=bridge.as_source_bridge(),
    )
    try:
        for entry in projected:
            cls = effective_spec.get_object_type(entry["object_type"])
            rt.put_object(cls(**entry["data"]))

        workload_res = run_standard_crm_workload(rt)

        # Direct independent verification against physical SQLite tables
        ids = workload_res["selected_ids"]
        raw_blocked_deal = bridge.fetch_raw_row("CrmDeal", ids["blocked_deal_id"])
        raw_crit_ticket = bridge.fetch_raw_row("CrmTicket", ids["critical_ticket_id"])
        raw_big_deal = bridge.fetch_raw_row("CrmDeal", ids["big_loss_deal_id"])
        raw_med_deal = bridge.fetch_raw_row("CrmDeal", ids["medium_case_deal_id"])

        deal_em = mapping.get_entity_mapping("CrmDeal")
        ticket_em = mapping.get_entity_mapping("CrmTicket")
        assert deal_em is not None and ticket_em is not None
        deal_stage_col = next(ft.source_field for ft in deal_em.field_transforms if ft.target_field == "stage")
        tk_status_col = next(ft.source_field for ft in ticket_em.field_transforms if ft.target_field == "status")

        return {
            "device": device_label,
            "scout_metrics": {
                "total_sqlite_tables": scout["total_sqlite_tables"],
                "core_tables": [e.table_name for e in profile.entities],
                "raw_domain_columns_total": scout["raw_domain_columns_total"],
                "scouted_table_columns": scout["scouted_table_columns"],
                "projected_m0_count": len(projected),
            },
            "decompilation": {
                "verified": dec_res.verified,
                "rounds_used": len(dec_res.rounds),
                "elapsed_sec": elapsed_sec,
                "round_traces": [
                    {
                        "round": tr.round_index,
                        "passed": tr.passed,
                        "failed_gates": list(tr.failed_gates),
                    }
                    for tr in dec_res.rounds
                ],
                "adapter_mapping": mapping.model_dump(mode="json"),
            },
            "workload_scenarios": workload_res,
            "physical_sqlite_verification": {
                "blocked_deal_native_stage": raw_blocked_deal.get(deal_stage_col),
                "critical_ticket_native_status": raw_crit_ticket.get(tk_status_col),
                "big_loss_deal_native_stage": raw_big_deal.get(deal_stage_col),
                "medium_deal_native_stage": raw_med_deal.get(deal_stage_col),
                "all_runs_sync_terminal": all(
                    r["status"] in {"COMMITTED", "REJECTED", "REVERTED"}
                    for r in rt.list_action_runs()
                ),
            },
        }
    finally:
        rt.close()
        bridge.close()


def run_dual_device_driver_swap_benchmark(
    *,
    bottlecrm_source: Path = BOTTLECRM_SQLITE_PATH,
    offline: bool = True,
    model: str = "openrouter/openai/gpt-4.1",
    max_rounds: int = 4,
    backend_a: AbductionBackend | None = None,
    backend_b: AbductionBackend | None = None,
) -> dict[str, Any]:
    """Run full dual-device hot-swap benchmark under one frozen ``std_crm + CustomerOverlay`` ABI."""
    source_sha_before = file_sha256(bottlecrm_source)
    locked_blueprint = build_locked_crm_target_blueprint()
    locked_fp = blueprint_fingerprint(locked_blueprint)
    effective_spec = compile_blueprint(locked_blueprint, seed_ontology=BUSINESS_SEED_ONTOLOGY)

    if offline:
        live_backend_a: AbductionBackend = backend_a or _offline_backend_for_mapping(build_offline_device_a_mapping())
        live_backend_b: AbductionBackend = backend_b or _offline_backend_for_mapping(build_offline_device_b_mapping())
        mode_label = "offline-reference"
    else:
        shared_backend, mode_label = _make_backend(model)
        assert shared_backend is not None
        live_backend_a = backend_a or shared_backend
        live_backend_b = backend_b or shared_backend

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        dev_a_db = tmp / "device_a_bottlecrm.sqlite3"
        shutil.copy2(bottlecrm_source, dev_a_db)

        dev_b_db = tmp / "device_b_heterogeneous.sqlite3"
        build_device_b_heterogeneous_sqlite(dev_b_db)

        res_a = _decompile_and_execute_on_device(
            device_label="device_a_bottlecrm",
            model_id="std_crm_enterprise_unified",
            db_path=dev_a_db,
            locked_blueprint=locked_blueprint,
            effective_spec=effective_spec,
            backend_override=live_backend_a,
            max_rounds=max_rounds,
        )
        res_b = _decompile_and_execute_on_device(
            device_label="device_b_heterogeneous_crm",
            model_id="std_crm_enterprise_unified",
            db_path=dev_b_db,
            locked_blueprint=locked_blueprint,
            effective_spec=effective_spec,
            backend_override=live_backend_b,
            max_rounds=max_rounds,
        )

    source_sha_after = file_sha256(bottlecrm_source)
    return {
        "mode": mode_label,
        "locked_abi_model_id": locked_blueprint.model_id,
        "locked_abi_version": locked_blueprint.version,
        "locked_abi_fingerprint": locked_fp,
        "bottlecrm_source_sha256": source_sha_after,
        "bottlecrm_source_unmodified": source_sha_before == source_sha_after,
        "device_a": res_a,
        "device_b": res_b,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="WO-07 Dual-Device Heterogeneous Driver Swap Benchmark"
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--model", default="openrouter/openai/gpt-4.1")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args(argv)

    result = run_dual_device_driver_swap_benchmark(
        offline=args.offline,
        model=args.model,
        max_rounds=args.max_rounds,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
