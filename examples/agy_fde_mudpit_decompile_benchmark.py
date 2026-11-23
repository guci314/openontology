"""WO-11 Phase C: Isolated agy CLI AI-FDE Mudpit Decompilation Benchmark & Anti-Cheat Verifier.

Sets up a physically isolated workspace where:
- Target Blueprint ABI (``target_abi.json``), business policy spec (``requirements.md``),
  Skill instructions (``SKILL.md``), cloned BottleCRM SQLite (``data/crm_sample.sqlite3``),
  dirty integer-PK ERP SQLite (``data/erp_sample.sqlite3``), device bus descriptors, and
  ``verify_driver.sh`` are strictly read-only (``0444``/``0555``);
- The external Coding Agent (``agy`` CLI) may ONLY explore the unknown databases and write
  4 declarative JSON artifacts into ``out/``, iterating against ``./verify_driver.sh``
  until ``CORROBORATED``;
- An independent outer referee verifies zero tampering on read-only inputs, validates
  anti-cheat thresholds on ``entity_resolution.json``, re-verifies the artifacts on fresh
  untouched SQLite clones across Round 1 (L4 gray zone detected), Steward ``WorkCard``
  adjudication, and Round 2 (``0`` ambiguous candidates + coupled ``DeviceDriverRegistry``
  cross-system Urgent-ticket runtime scenarios), writing full logs to
  ``/tmp/wo11_agy_fde_live.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from open_ontology import Actor, DeviceDriverRegistry, InvariantError, MountedDevice, OntologyRuntime  # noqa: E402
from open_ontology.design import DeclarativeSQLiteDriverBridge, run_design_check, run_design_verify, scout_readonly_sqlite_profile  # noqa: E402
from open_ontology.induction import (  # noqa: E402
    ActionExecutionBinding,
    AdapterMappingSpec,
    CustomerOverlaySpec,
    DeclarativeBlueprint,
    EntityMappingSpec,
    EntityResolutionDecision,
    EntityResolutionSpec,
    FieldTransformSpec,
    InvariantRuleSpec,
    SourceEntityBatch,
    SourceRef,
    apply_overlay,
    apply_resolution_decisions,
    build_resolution_task_intent,
    compile_blueprint,
    compose_blueprints,
    inject_crosswalk_into_mapping,
    merge_projected_phenomena,
    project_external_phenomenon,
    resolve_entities,
)
from examples.business_ontology import BUSINESS_SEED_ONTOLOGY, WorkCard  # noqa: E402
from examples.std_core_ontology import build_std_core_blueprint  # noqa: E402
from examples.std_crm_ontology import build_std_crm_blueprint  # noqa: E402
from examples.std_o2c_ontology import build_std_o2c_blueprint  # noqa: E402

BOTTLECRM_SOURCE_DB = ROOT / "vendor_crm" / "bottlecrm.sqlite3"
SKILL_MD_PATH = ROOT / "skills" / "openontology-fde" / "SKILL.md"
ACME_CRM_UUID = "39e36940179f47c7be22805d72db1a99"
NORTHWIND_CRM_UUID = "9865ac3c44d1493da0a88913d2458f52"
SOUTHSTAR_CRM_UUID = "3fe3cc2bed70479ea9ed418d2667e06d"
SOUTHSTAR_URGENT_TICKET_UUID = "9138b7c3d1a048fcb67aea094b4f57f4"


def build_locked_enterprise_target_blueprint() -> DeclarativeBlueprint:
    """Return locked composed ``std_core + std_crm + std_o2c`` Target Blueprint ABI."""
    return compose_blueprints(
        [
            build_std_core_blueprint(),
            build_std_crm_blueprint(),
            build_std_o2c_blueprint(),
        ],
        model_id="std_enterprise_crm_o2c",
        version="1.0.0",
        seed_ontology_id="business_ontology",
        description="Composed Enterprise Standard ABI (Core + CRM + O2C).",
    )


def build_heterogeneous_dirty_erp_sqlite(db_path: Path) -> Path:
    """Create a 30-table dirty integer-PK ERP SQLite with 25 noise tables and proprietary enums."""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        for idx in range(1, 26):
            tname = f"noise_erp_legacy_{idx:02d}"
            conn.execute(
                f'CREATE TABLE "{tname}" ("nid" INTEGER PRIMARY KEY, "blob_val" TEXT, "flag" INTEGER)'
            )
            for row_i in range(1, 3):
                conn.execute(
                    f'INSERT INTO "{tname}" ("nid", "blob_val", "flag") VALUES (?, ?, ?)',
                    (row_i, f"legacy-junk-{idx}-{row_i}", row_i % 2),
                )

        conn.execute(
            """
            CREATE TABLE "biz_legal_entities" (
                "comp_pk" INTEGER PRIMARY KEY,
                "comp_code" TEXT NOT NULL,
                "comp_title" TEXT NOT NULL,
                "base_ccy" TEXT NOT NULL DEFAULT 'CNY',
                "is_live" INTEGER NOT NULL DEFAULT 1,
                "sync_ver" INTEGER NOT NULL DEFAULT 1,
                "legacy_blob" TEXT DEFAULT '{}'
            )
            """
        )
        conn.execute(
            'INSERT INTO "biz_legal_entities" ("comp_pk", "comp_code", "comp_title", "base_ccy", "is_live") '
            "VALUES (10, 'ENT-MAIN', 'China Operations HQ', 'CNY', 1)"
        )

        conn.execute(
            """
            CREATE TABLE "erp_trade_partners" (
                "ptn_pk" INTEGER PRIMARY KEY,
                "ptn_title" TEXT NOT NULL,
                "tax_code" TEXT NOT NULL DEFAULT '',
                "iso_region" TEXT NOT NULL DEFAULT 'CN',
                "credit_cap" REAL NOT NULL DEFAULT 0.0,
                "pay_term" TEXT NOT NULL DEFAULT 'NET30',
                "company_fk" INTEGER NOT NULL REFERENCES "biz_legal_entities" ("comp_pk"),
                "ui_flag" INTEGER NOT NULL DEFAULT 0,
                "garbage_note" TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.executemany(
            'INSERT INTO "erp_trade_partners" ("ptn_pk", "ptn_title", "tax_code", "iso_region", "credit_cap", "pay_term", "company_fk", "garbage_note") '
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                # L1 Hard Key merge with CRM Acme (totally different Chinese name, hyphenated tax_code)
                (1001, "艾克米工业集团（中国）", "91310000-ACME-0001X", "CN", 200000.0, "NET30", 10, "L1 Acme"),
                # L4 Gray Zone with CRM Northwind (missing tax_code, similar name + CN region)
                (1002, "Northwind Trading Labs", "", "CN", 150000.0, "NET30", 10, "L4 Northwind"),
                # L1 Hard Key merge with CRM Southstar HQ (same tax_code as CRM Southstar HQ)
                (1003, "Southstar Medical Co., Ltd.", "91440100SOUTH8888A", "CN", 300000.0, "NET30", 10, "L1 Southstar HQ"),
                # Hard Key CONFLICT with Southstar HQ (identical name, different subsidiary tax_code)
                (1004, "Southstar Medical", "91440300DIFF9999Z", "CN", 300000.0, "NET30", 10, "Conflict Sub"),
            ],
        )

        conn.execute(
            """
            CREATE TABLE "erp_sales_orders" (
                "ord_pk" INTEGER PRIMARY KEY,
                "doc_ref" TEXT NOT NULL,
                "ptn_fk" INTEGER NOT NULL REFERENCES "erp_trade_partners" ("ptn_pk"),
                "company_fk" INTEGER NOT NULL REFERENCES "biz_legal_entities" ("comp_pk"),
                "gross_amt" REAL NOT NULL,
                "ord_stage" TEXT NOT NULL CHECK (
                    "ord_stage" IN ('erp_draft', 'erp_confirmed', 'erp_shipped', 'erp_billed', 'erp_void')
                ),
                "sync_ver" INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.executemany(
            'INSERT INTO "erp_sales_orders" ("ord_pk", "doc_ref", "ptn_fk", "company_fk", "gross_amt", "ord_stage") '
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (2001, "SO-ERP-2001", 1001, 10, 25000.0, "erp_draft"),
                (2002, "SO-ERP-2002", 1003, 10, 40000.0, "erp_draft"),
                (2003, "SO-ERP-2003", 1004, 10, 35000.0, "erp_draft"),
                (2004, "SO-ERP-2004", 1001, 10, 18000.0, "erp_confirmed"),
                (2005, "SO-ERP-2005", 1002, 10, 12000.0, "erp_shipped"),
            ],
        )

        conn.execute(
            """
            CREATE TABLE "erp_shipments" (
                "shp_pk" INTEGER PRIMARY KEY,
                "ord_fk" INTEGER NOT NULL REFERENCES "erp_sales_orders" ("ord_pk"),
                "shp_state" TEXT NOT NULL CHECK (
                    "shp_state" IN ('shp_wait', 'shp_done', 'shp_cancel')
                ),
                "sync_ver" INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            'INSERT INTO "erp_shipments" ("shp_pk", "ord_fk", "shp_state") VALUES (3001, 2004, \'shp_wait\')'
        )

        conn.execute(
            """
            CREATE TABLE "erp_ar_invoices" (
                "inv_pk" INTEGER PRIMARY KEY,
                "ord_fk" INTEGER NOT NULL REFERENCES "erp_sales_orders" ("ord_pk"),
                "bill_total" REAL NOT NULL,
                "open_residual" REAL NOT NULL,
                "pay_flag" TEXT NOT NULL CHECK (
                    "pay_flag" IN ('ar_unpaid', 'ar_part', 'ar_settled')
                ),
                "inv_phase" TEXT NOT NULL CHECK (
                    "inv_phase" IN ('inv_draft', 'inv_posted', 'inv_void')
                ),
                "sync_ver" INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.executemany(
            'INSERT INTO "erp_ar_invoices" ("inv_pk", "ord_fk", "bill_total", "open_residual", "pay_flag", "inv_phase") '
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (4001, 2005, 12000.0, 12000.0, "ar_unpaid", "inv_draft"),
                (4002, 2005, 5000.0, 5000.0, "ar_unpaid", "inv_posted"),
            ],
        )

        conn.commit()
    finally:
        conn.close()
    return db_path


def build_mudpit_crm_and_erp_sqlite_pair(target_dir: Path) -> dict[str, Path]:
    """Build cloned BottleCRM + heterogeneous dirty ERP SQLite inside ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    crm_db = target_dir / "crm_sample.sqlite3"
    shutil.copy2(BOTTLECRM_SOURCE_DB, crm_db)
    os.chmod(crm_db, 0o600)
    conn = sqlite3.connect(crm_db)
    try:
        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("accounts")').fetchall()}
        if "tax_reg_no" not in cols:
            conn.execute('ALTER TABLE "accounts" ADD COLUMN "tax_reg_no" TEXT DEFAULT \'\'')
        conn.execute(
            'UPDATE "accounts" SET "tax_reg_no" = ? WHERE "id" = ?',
            ("91310000ACME0001X", ACME_CRM_UUID),
        )
        conn.execute(
            'UPDATE "accounts" SET "tax_reg_no" = ? WHERE "id" = ?',
            ("91440100SOUTH8888A", SOUTHSTAR_CRM_UUID),
        )
        conn.execute(
            'UPDATE "accounts" SET "tax_reg_no" = \'\' WHERE "id" = ?',
            (NORTHWIND_CRM_UUID,),
        )
        conn.commit()
    finally:
        conn.close()

    erp_db = build_heterogeneous_dirty_erp_sqlite(target_dir / "erp_sample.sqlite3")
    return {"crm_db": crm_db, "erp_db": erp_db}


def build_reference_crm_mapping() -> AdapterMappingSpec:
    """Reference offline ``AdapterMappingSpec`` for BottleCRM clone -> CRM subset + BusinessParty."""
    return AdapterMappingSpec(
        mapping_id="mudpit_bottlecrm_device",
        source_kind="sqlite",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="PTY-CRM-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="tax_id", source_field="tax_reg_no", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="country", kind="direct", default="CN"),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="accounts",
                target_object_type="CrmAccount",
                id_transform=FieldTransformSpec(
                    target_field="account_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="ACC-",
                ),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="party",
                        source_field="id",
                        kind="lookup_join",
                        lookup_table="accounts",
                        lookup_key_field="id",
                        lookup_target_type="BusinessParty",
                    ),
                    FieldTransformSpec(target_field="name", source_field="name", kind="direct", default=""),
                    FieldTransformSpec(target_field="industry", source_field="industry", kind="direct", default=""),
                    FieldTransformSpec(target_field="status", source_field="is_active", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="opportunity",
                target_object_type="CrmDeal",
                id_transform=FieldTransformSpec(
                    target_field="deal_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="DEAL-",
                ),
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
                id_transform=FieldTransformSpec(
                    target_field="ticket_id",
                    source_field="id",
                    kind="id_prefix",
                    prefix="TK-",
                ),
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


def build_reference_erp_mapping() -> AdapterMappingSpec:
    """Reference offline ``AdapterMappingSpec`` for dirty integer-PK ERP SQLite -> O2C + BusinessParty."""
    return AdapterMappingSpec(
        mapping_id="mudpit_dirty_erp_device",
        source_kind="sqlite",
        treat_false_as_null=True,
        entity_mappings=[
            EntityMappingSpec(
                source_table="biz_legal_entities",
                target_object_type="AccountingEntity",
                id_transform=FieldTransformSpec(
                    target_field="entity_id",
                    source_field="comp_pk",
                    kind="id_prefix",
                    prefix="ENT-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="name", source_field="comp_title", kind="direct", default=""),
                    FieldTransformSpec(target_field="base_currency", source_field="base_ccy", kind="direct", default="CNY"),
                    FieldTransformSpec(target_field="status", source_field="is_live", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="erp_trade_partners",
                target_object_type="BusinessParty",
                id_transform=FieldTransformSpec(
                    target_field="party_id",
                    source_field="ptn_pk",
                    kind="id_prefix",
                    prefix="PTY-ERP-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="legal_name", source_field="ptn_title", kind="direct", default=""),
                    FieldTransformSpec(target_field="tax_id", source_field="tax_code", kind="direct", default=""),
                    FieldTransformSpec(target_field="country_code", source_field="iso_region", kind="direct", default="CN"),
                    FieldTransformSpec(target_field="status", source_field="ptn_pk", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="erp_trade_partners",
                target_object_type="O2cCustomerProfile",
                id_transform=FieldTransformSpec(
                    target_field="profile_id",
                    source_field="ptn_pk",
                    kind="id_prefix",
                    prefix="PROF-",
                ),
                field_transforms=[
                    FieldTransformSpec(
                        target_field="party",
                        source_field="ptn_pk",
                        kind="lookup_join",
                        lookup_table="erp_trade_partners",
                        lookup_key_field="ptn_pk",
                        lookup_target_type="BusinessParty",
                    ),
                    FieldTransformSpec(
                        target_field="accounting_entity",
                        source_field="company_fk",
                        kind="foreign_key_id",
                        prefix="ENT-",
                    ),
                    FieldTransformSpec(target_field="credit_limit", source_field="credit_cap", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="payment_terms", source_field="pay_term", kind="direct", default="NET30"),
                    FieldTransformSpec(target_field="status", source_field="ptn_pk", kind="const", default="active"),
                ],
            ),
            EntityMappingSpec(
                source_table="erp_sales_orders",
                target_object_type="O2cOrder",
                id_transform=FieldTransformSpec(
                    target_field="order_id",
                    source_field="ord_pk",
                    kind="id_prefix",
                    prefix="ORD-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order_number", source_field="doc_ref", kind="direct", default=""),
                    FieldTransformSpec(target_field="customer", source_field="ptn_fk", kind="foreign_key_id", prefix="PROF-"),
                    FieldTransformSpec(target_field="accounting_entity", source_field="company_fk", kind="foreign_key_id", prefix="ENT-"),
                    FieldTransformSpec(target_field="amount_total", source_field="gross_amt", kind="direct", default=0.0),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="ord_stage",
                        kind="enum_map",
                        default="draft",
                        enum_map={
                            "erp_draft": "draft",
                            "erp_confirmed": "confirmed",
                            "erp_shipped": "delivered",
                            "erp_billed": "invoiced",
                            "erp_void": "cancelled",
                        },
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="erp_shipments",
                target_object_type="O2cDelivery",
                id_transform=FieldTransformSpec(
                    target_field="delivery_id",
                    source_field="shp_pk",
                    kind="id_prefix",
                    prefix="DEL-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order", source_field="ord_fk", kind="foreign_key_id", prefix="ORD-"),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="shp_state",
                        kind="enum_map",
                        default="pending",
                        enum_map={
                            "shp_wait": "pending",
                            "shp_done": "completed",
                            "shp_cancel": "cancelled",
                        },
                    ),
                ],
            ),
            EntityMappingSpec(
                source_table="erp_ar_invoices",
                target_object_type="O2cInvoice",
                id_transform=FieldTransformSpec(
                    target_field="invoice_id",
                    source_field="inv_pk",
                    kind="id_prefix",
                    prefix="INV-",
                ),
                field_transforms=[
                    FieldTransformSpec(target_field="order", source_field="ord_fk", kind="foreign_key_id", prefix="ORD-"),
                    FieldTransformSpec(target_field="amount_total", source_field="bill_total", kind="direct", default=0.0),
                    FieldTransformSpec(target_field="amount_residual", source_field="open_residual", kind="direct", default=0.0),
                    FieldTransformSpec(
                        target_field="payment_state",
                        source_field="pay_flag",
                        kind="enum_map",
                        default="not_paid",
                        enum_map={
                            "ar_unpaid": "not_paid",
                            "ar_part": "partial",
                            "ar_settled": "paid",
                        },
                    ),
                    FieldTransformSpec(
                        target_field="status",
                        source_field="inv_phase",
                        kind="enum_map",
                        default="draft",
                        enum_map={
                            "inv_draft": "draft",
                            "inv_posted": "posted",
                            "inv_void": "cancelled",
                        },
                    ),
                ],
            ),
        ],
        action_bindings=[
            ActionExecutionBinding(
                action_name="confirm_o2c_order",
                external_table="erp_sales_orders",
                external_method="ord_stage=erp_confirmed",
                target_object_type="O2cOrder",
                id_param="order_id",
                id_prefix="ORD-",
                expected_state_field="status",
                expected_state_after="confirmed",
            ),
            ActionExecutionBinding(
                action_name="complete_o2c_delivery",
                external_table="erp_shipments",
                external_method="shp_state=shp_done",
                target_object_type="O2cDelivery",
                id_param="delivery_id",
                id_prefix="DEL-",
                expected_state_field="status",
                expected_state_after="completed",
            ),
            ActionExecutionBinding(
                action_name="post_o2c_invoice",
                external_table="erp_ar_invoices",
                external_method="inv_phase=inv_posted",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
                id_prefix="INV-",
                expected_state_field="status",
                expected_state_after="posted",
            ),
            ActionExecutionBinding(
                action_name="record_o2c_payment",
                external_table="erp_ar_invoices",
                external_method="pay_flag=ar_settled",
                target_object_type="O2cInvoice",
                id_param="invoice_id",
                id_prefix="INV-",
                expected_state_field="payment_state",
                expected_state_after="paid",
            ),
        ],
    )


def build_reference_entity_resolution_spec() -> EntityResolutionSpec:
    return EntityResolutionSpec(
        target_object_type="BusinessParty",
        canonical_id_prefix="PTY-",
        hard_key_fields=["tax_id"],
        quasi_key_fields=["legal_name"],
        neighbor_match_fields=["country_code"],
        strip_tokens=["Co., Ltd.", "Ltd.", "Inc.", "Corp.", "有限公司", "有限责任公司", "股份有限公司"],
        auto_merge_threshold=0.90,
        review_threshold=0.55,
        quasi_key_weight=0.70,
        neighbor_weight=0.30,
        survivorship={
            "legal_name": ["device_crm", "device_erp"],
            "tax_id": ["device_erp", "device_crm"],
            "country_code": ["device_erp", "device_crm"],
        },
    )


def build_reference_customer_overlay() -> CustomerOverlaySpec:
    return CustomerOverlaySpec(
        overlay_id="mudpit_cross_system_urgent_ticket_guard",
        base_model_id="std_enterprise_crm_o2c",
        target_version="1.1.0",
        description="Block O2C order confirmation when shared BusinessParty has an OPEN/IN_PROGRESS URGENT CRM ticket.",
        add_action_preconditions={
            "confirm_o2c_order": [
                InvariantRuleSpec(
                    name="block_o2c_order_on_open_urgent_crm_ticket",
                    code="OPEN_URGENT_CRM_TICKET_BLOCKS_ORDER",
                    target_type="O2cOrder",
                    id_param="order_id",
                    must_hold={
                        "op": "not_exists",
                        "object_type": "CrmTicket",
                        "where": {
                            "op": "and",
                            "args": [
                                {
                                    "op": "eq",
                                    "left": {"ref": "target.account.party"},
                                    "right": {"ref": "obj.customer.party"},
                                },
                                {
                                    "op": "eq",
                                    "left": {"ref": "target.priority"},
                                    "right": {"lit": "URGENT"},
                                },
                                {
                                    "op": "in",
                                    "left": {"ref": "target.status"},
                                    "right": {"lit": ["OPEN", "IN_PROGRESS"]},
                                },
                            ],
                        },
                    },
                    description="Do not confirm O2cOrder when the shared BusinessParty has an open URGENT CrmTicket.",
                )
            ]
        },
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def prepare_isolated_player_workspace(player_ws: Path) -> dict[str, Any]:
    """Build a strictly isolated player workspace with chmod 0444 read-only fixtures and ./verify_driver.sh."""
    player_ws.mkdir(parents=True, exist_ok=True)
    data_dir = player_ws / "data"
    judge_dir = player_ws / ".judge"
    out_dir = player_ws / "out"
    data_dir.mkdir(exist_ok=True)
    judge_dir.mkdir(exist_ok=True)
    out_dir.mkdir(exist_ok=True)

    dbs = build_mudpit_crm_and_erp_sqlite_pair(data_dir)
    crm_db = dbs["crm_db"]
    erp_db = dbs["erp_db"]

    target_abi = build_locked_enterprise_target_blueprint()
    target_abi_path = player_ws / "target_abi.json"
    target_abi_path.write_text(
        json.dumps(target_abi.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    skill_dest = player_ws / "SKILL.md"
    skill_dest.write_text(SKILL_MD_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    # Zero table names / column names / raw enum tokens leaked in requirements.md!
    requirements_md = """# Customer Integration Requirements (Isolated AI-FDE Task)

Two heterogeneous external systems are available under `data/`:
- `data/crm_sample.sqlite3`: CRM subsystem mounted on the device bus with `device_id="device_crm"`
- `data/erp_sample.sqlite3`: O2C/ERP subsystem mounted on the device bus with `device_id="device_erp"`

Note: Both SQLite files may contain many unused, empty, or legacy noise tables and noisy columns. Inspect the schemas and rows autonomously using read-only SQLite queries (`sqlite_master`, `PRAGMA table_info`, `PRAGMA foreign_key_list`, sample `SELECT` rows) to identify the real entities, primary/foreign keys, and proprietary status/priority/payment tokens.

## Required Deliverables (Write ONLY inside `out/`)

1. **`out/device_crm_mapping.json`** (`AdapterMappingSpec`):
   - Map CRM system data onto `BusinessParty`, `CrmAccount`, `CrmDeal`, and `CrmTicket` matching `target_abi.json`.
   - Bind all standard CRM source actions: `win_crm_deal`, `lose_crm_deal`, and `resolve_crm_ticket`.
   - Ensure bidirectional `enum_map` entries cover all observed values AND all reachable target states.

2. **`out/device_erp_mapping.json`** (`AdapterMappingSpec`):
   - Map ERP system data onto `AccountingEntity`, `BusinessParty`, `O2cCustomerProfile`, `O2cOrder`, `O2cDelivery`, and `O2cInvoice` matching `target_abi.json`.
   - Bind all standard O2C source actions: `confirm_o2c_order`, `complete_o2c_delivery`, `post_o2c_invoice`, and `record_o2c_payment`.
   - Ensure every proprietary enum field has complete bidirectional `enum_map` coverage for observed AND target post-action states.

3. **`out/entity_resolution.json`** (`EntityResolutionSpec`):
   - Align `BusinessParty` across `device_crm` and `device_erp` with `canonical_id_prefix="PTY-"`.
   - Use canonical tax ID (`tax_id`) as the L1 hard key (so normalized matching tax IDs merge even across differing names/languages, while conflicting non-empty tax IDs NEVER merge even when names match).
   - Use canonical legal name (`legal_name`) as L2 quasi-key with corporate suffix tokens in `strip_tokens` (e.g., `Co., Ltd.`, `Ltd.`, `Inc.`, `Corp.`, `有限公司`, `有限责任公司`, `股份有限公司`), and canonical country (`country_code`) as L3 neighbor field.
   - Keep standard safety thresholds (`auto_merge_threshold=0.90`, `review_threshold=0.55`, `quasi_key_weight=0.70`, `neighbor_weight=0.30`) so ambiguous entities missing a hard key are NOT forced to auto-merge and remain visible in L4 `ambiguous_candidates`.
   - Set `survivorship` so `legal_name` prefers `["device_crm", "device_erp"]`, while `tax_id` and `country_code` prefer `["device_erp", "device_crm"]`.

4. **`out/customer_overlay.json`** (`CustomerOverlaySpec`):
   - Base model: `std_enterprise_crm_o2c`.
   - Add a cross-system guardrail precondition on `confirm_o2c_order`:
     - Error code: `OPEN_URGENT_CRM_TICKET_BLOCKS_ORDER`
     - Rule: Block confirming an `O2cOrder` whenever there exists any `CrmTicket` sharing the same canonical `BusinessParty` (via `target.account.party == obj.customer.party`) whose `priority` is `URGENT` and whose `status` is `OPEN` or `IN_PROGRESS`.
   - Keep standard contracts strictly monotonic (no destructive keys or expanded standard roles).

## Acceptance Gate
Run `./verify_driver.sh check` and `./verify_driver.sh verify` cleanly until `"verdict": "CORROBORATED"` and exit code `0`.
"""
    req_path = player_ws / "requirements.md"
    req_path.write_text(requirements_md, encoding="utf-8")

    # Device descriptors specify ONLY logical bus ownership (Target ABI types/actions) + paths, never SQL schemas
    dev_crm_cfg = {
        "device_id": "device_crm",
        "name": "CRM Device",
        "kind": "sqlite",
        "db_path": "data/crm_sample.sqlite3",
        "mapping_path": "out/device_crm_mapping.json",
        "handled_actions": ["win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"],
        "owned_object_types": ["CrmAccount", "CrmDeal", "CrmTicket"],
        "shared_object_types": ["BusinessParty"],
    }
    dev_erp_cfg = {
        "device_id": "device_erp",
        "name": "ERP Device",
        "kind": "sqlite",
        "db_path": "data/erp_sample.sqlite3",
        "mapping_path": "out/device_erp_mapping.json",
        "handled_actions": [
            "confirm_o2c_order",
            "complete_o2c_delivery",
            "post_o2c_invoice",
            "record_o2c_payment",
        ],
        "owned_object_types": [
            "AccountingEntity",
            "O2cCustomerProfile",
            "O2cOrder",
            "O2cDelivery",
            "O2cInvoice",
        ],
        "shared_object_types": ["BusinessParty"],
    }
    dev_crm_cfg_path = judge_dir / "device_crm.json"
    dev_erp_cfg_path = judge_dir / "device_erp.json"
    dev_crm_cfg_path.write_text(json.dumps(dev_crm_cfg, indent=2), encoding="utf-8")
    dev_erp_cfg_path.write_text(json.dumps(dev_erp_cfg, indent=2), encoding="utf-8")

    py_exe = sys.executable
    verify_sh = f"""#!/bin/sh
set -eu
CMD="${{1:-verify}}"
if [ "$#" -gt 0 ]; then
  shift
fi
export PYTHONPATH="{ROOT}"
exec "{py_exe}" -m open_ontology.design "$CMD" \\
  --blueprint examples.agy_fde_mudpit_decompile_benchmark:build_locked_enterprise_target_blueprint \\
  --overlay out/customer_overlay.json \\
  --seed examples.business_ontology:BUSINESS_SEED_ONTOLOGY \\
  --device .judge/device_crm.json \\
  --device .judge/device_erp.json \\
  --resolution-spec out/entity_resolution.json \\
  "$@"
"""
    verify_sh_path = player_ws / "verify_driver.sh"
    verify_sh_path.write_text(verify_sh, encoding="utf-8")

    readonly_files = [
        crm_db,
        erp_db,
        target_abi_path,
        skill_dest,
        req_path,
        dev_crm_cfg_path,
        dev_erp_cfg_path,
    ]
    for fpath in readonly_files:
        os.chmod(fpath, 0o444)
    os.chmod(verify_sh_path, 0o555)

    hashes = {str(p.relative_to(player_ws)): _sha256(p) for p in [*readonly_files, verify_sh_path]}
    return {
        "player_ws": player_ws,
        "readonly_hashes": hashes,
        "bottlecrm_source_sha256": _sha256(BOTTLECRM_SOURCE_DB),
    }


def _independent_referee_audit(
    *,
    player_ws: Path,
    readonly_hashes_before: dict[str, str],
    bottlecrm_sha_before: str,
) -> dict[str, Any]:
    """Independently re-verify player out/*.json on fresh untouched SQLite clones (anti-cheat)."""
    # 1. Tamper checks
    tampered: list[str] = []
    for rel, old_hash in readonly_hashes_before.items():
        p = player_ws / rel
        if not p.is_file() or _sha256(p) != old_hash:
            tampered.append(rel)
    assert not tampered, f"Read-only player workspace files were modified: {tampered}"
    assert _sha256(BOTTLECRM_SOURCE_DB) == bottlecrm_sha_before, "Source BottleCRM DB was modified!"

    # 2. Load player outputs
    out_dir = player_ws / "out"
    required_files = [
        "device_crm_mapping.json",
        "device_erp_mapping.json",
        "entity_resolution.json",
        "customer_overlay.json",
    ]
    missing = [fn for fn in required_files if not (out_dir / fn).is_file()]
    assert not missing, f"Player missing required outputs in out/: {missing}"

    crm_map = AdapterMappingSpec.model_validate(json.loads((out_dir / "device_crm_mapping.json").read_text("utf-8")))
    erp_map = AdapterMappingSpec.model_validate(json.loads((out_dir / "device_erp_mapping.json").read_text("utf-8")))
    er_spec = EntityResolutionSpec.model_validate(json.loads((out_dir / "entity_resolution.json").read_text("utf-8")))
    overlay = CustomerOverlaySpec.model_validate(json.loads((out_dir / "customer_overlay.json").read_text("utf-8")))

    # Anti-cheat guard: player must NOT lower auto_merge_threshold to dodge L4 gray zone
    assert er_spec.auto_merge_threshold >= 0.85, (
        f"Anti-cheat failed: auto_merge_threshold lowered to {er_spec.auto_merge_threshold}"
    )
    assert er_spec.review_threshold <= 0.65, (
        f"Anti-cheat failed: review_threshold raised to {er_spec.review_threshold}"
    )

    with tempfile.TemporaryDirectory(prefix="wo11_referee_") as ref_tmp:
        ref_dir = Path(ref_tmp)
        ref_dbs = build_mudpit_crm_and_erp_sqlite_pair(ref_dir / "data")
        target_bp = build_locked_enterprise_target_blueprint()
        abi_file = ref_dir / "target_abi.json"
        abi_file.write_text(json.dumps(target_bp.model_dump(mode="json"), indent=2), "utf-8")
        ov_file = ref_dir / "overlay.json"
        ov_file.write_text(json.dumps(overlay.model_dump(mode="json"), indent=2), "utf-8")
        er_file = ref_dir / "er.json"
        er_file.write_text(json.dumps(er_spec.model_dump(mode="json"), indent=2), "utf-8")
        crm_map_file = ref_dir / "crm_map.json"
        crm_map_file.write_text(json.dumps(crm_map.model_dump(mode="json"), indent=2), "utf-8")
        erp_map_file = ref_dir / "erp_map.json"
        erp_map_file.write_text(json.dumps(erp_map.model_dump(mode="json"), indent=2), "utf-8")

        dev_crm_file = ref_dir / "dev_crm.json"
        dev_crm_file.write_text(
            json.dumps(
                {
                    "device_id": "device_crm",
                    "name": "Referee CRM",
                    "kind": "sqlite",
                    "db_path": str(ref_dbs["crm_db"]),
                    "mapping_path": str(crm_map_file),
                    "handled_actions": ["win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"],
                    "owned_object_types": ["CrmAccount", "CrmDeal", "CrmTicket"],
                    "shared_object_types": ["BusinessParty"],
                }
            ),
            "utf-8",
        )
        dev_erp_file = ref_dir / "dev_erp.json"
        dev_erp_file.write_text(
            json.dumps(
                {
                    "device_id": "device_erp",
                    "name": "Referee ERP",
                    "kind": "sqlite",
                    "db_path": str(ref_dbs["erp_db"]),
                    "mapping_path": str(erp_map_file),
                    "handled_actions": [
                        "confirm_o2c_order",
                        "complete_o2c_delivery",
                        "post_o2c_invoice",
                        "record_o2c_payment",
                    ],
                    "owned_object_types": [
                        "AccountingEntity",
                        "O2cCustomerProfile",
                        "O2cOrder",
                        "O2cDelivery",
                        "O2cInvoice",
                    ],
                    "shared_object_types": ["BusinessParty"],
                }
            ),
            "utf-8",
        )

        # Round 1 Verify: must be CORROBORATED, with L1 merges, Southstar conflict blocked, and Northwind in L4
        round1 = run_design_verify(
            blueprints=["examples.agy_fde_mudpit_decompile_benchmark:build_locked_enterprise_target_blueprint"],
            overlay=str(ov_file),
            seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
            devices=[str(dev_crm_file), str(dev_erp_file)],
            resolution_spec=str(er_file),
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=3,
        )
        assert round1["ok"] is True and round1["verdict"] == "CORROBORATED", round1
        res_sum1 = round1["resolution_summary"]
        assert res_sum1 is not None
        assert any(bp["reason_code"] == "CONFLICTING_HARD_KEY" for bp in res_sum1["blocked_pairs"]), res_sum1
        assert len(res_sum1["ambiguous_candidates"]) >= 1, "Expected L4 gray-zone candidate (Northwind)"

        # Steward L4 adjudication via real M1 WorkCard(card_kind="HUMAN_TASK")
        eff_bp = apply_overlay(target_bp, overlay)
        compiled_spec = compile_blueprint(eff_bp, seed_ontology=BUSINESS_SEED_ONTOLOGY)
        rt_wc = OntologyRuntime(compiled_spec, ":memory:")
        try:
            crm_bp_em = crm_map.get_entity_mapping("BusinessParty")
            erp_bp_em = erp_map.get_entity_mapping("BusinessParty")
            assert crm_bp_em is not None and erp_bp_em is not None
            crm_prof = scout_readonly_sqlite_profile(ref_dbs["crm_db"], include_tables=[crm_bp_em.source_table])
            erp_prof = scout_readonly_sqlite_profile(ref_dbs["erp_db"], include_tables=[erp_bp_em.source_table])
            er_batches = [
                SourceEntityBatch(
                    source_id="device_crm",
                    source_table=crm_bp_em.source_table,
                    pk_field=crm_bp_em.id_transform.source_field or "id",
                    field_map={tf.target_field: tf.source_field for tf in crm_bp_em.field_transforms if tf.source_field},
                    records=next(e.sample_rows for e in crm_prof.entities if e.table_name == crm_bp_em.source_table),
                ),
                SourceEntityBatch(
                    source_id="device_erp",
                    source_table=erp_bp_em.source_table,
                    pk_field=erp_bp_em.id_transform.source_field or "ptn_pk",
                    field_map={tf.target_field: tf.source_field for tf in erp_bp_em.field_transforms if tf.source_field},
                    records=next(e.sample_rows for e in erp_prof.entities if e.table_name == erp_bp_em.source_table),
                ),
            ]
            er_r1 = resolve_entities(er_spec, er_batches)
            ops = Actor(actor_id="ops-1", role="ops")
            steward = Actor(actor_id="stew-1", role="steward")
            decisions_list: list[EntityResolutionDecision] = []
            for idx, cand in enumerate(er_r1.ambiguous_candidates, start=1):
                intent = build_resolution_task_intent(cand)
                cid = f"WC-L4-{idx}"
                rt_wc.submit_action(
                    f"OPEN-{cid}",
                    "open_work_card",
                    {
                        "card_id": cid,
                        "card_kind": intent["task_kind"],
                        "target_object_type": intent["target_object_type"],
                        "target_object_id": intent["target_object_id"],
                        "requested_action": intent["requested_operation"],
                        "requested_params_json": intent["params_json"],
                        "assignee_role": intent["suggested_assignee_role"],
                    },
                    ops,
                )
                rt_wc.submit_action(f"CLAIM-{cid}", "claim_work_card", {"card_id": cid}, steward)
                rt_wc.submit_action(
                    f"COMP-{cid}",
                    "complete_work_card",
                    {"card_id": cid, "resolution_note": "Approved L4 entity match"},
                    steward,
                )
                assert rt_wc.get_object(WorkCard, cid, steward).status == "COMPLETED"
                decisions_list.append(
                    EntityResolutionDecision(
                        candidate_id=cand.candidate_id,
                        left=cand.left,
                        right=cand.right,
                        verdict="CONFIRM_MERGE",
                        decided_by=steward.actor_id,
                        rationale="Steward confirmed L4 match",
                    )
                )
        finally:
            rt_wc.close()

        dec_file = ref_dir / "decisions.json"
        dec_file.write_text(
            json.dumps([d.model_dump(mode="json") for d in decisions_list], indent=2),
            "utf-8",
        )

        # Round 2 Verify with steward decisions -> 0 ambiguous candidates & CORROBORATED
        round2 = run_design_verify(
            blueprints=["examples.agy_fde_mudpit_decompile_benchmark:build_locked_enterprise_target_blueprint"],
            overlay=str(ov_file),
            seed="examples.business_ontology:BUSINESS_SEED_ONTOLOGY",
            devices=[str(dev_crm_file), str(dev_erp_file)],
            resolution_spec=str(er_file),
            decisions=str(dec_file),
            min_action_coverage=1.0,
            min_transition_coverage=0.80,
            max_waves=3,
        )
        assert round2["ok"] is True and round2["verdict"] == "CORROBORATED", round2
        res_sum2 = round2["resolution_summary"]
        assert res_sum2 is not None and res_sum2["ambiguous_candidates"] == [], res_sum2
        assert res_sum2["stats"]["l0_confirmed_entities"] >= 1, res_sum2

        # 3. Live coupled DeviceDriverRegistry business scenario check (Southstar HQ blocked, Southstar Sub allowed, Resolve Ticket -> Unblock HQ)
        cw_final = apply_resolution_decisions(er_r1.crosswalk, decisions_list)
        er_r2 = resolve_entities(er_spec, er_batches, existing_crosswalk=cw_final)
        fk_fields = [
            (ot.name, rel.field)
            for ot in eff_bp.object_types
            for rel in ot.relations
            if rel.target_type == "BusinessParty"
        ]
        crm_map_eff = inject_crosswalk_into_mapping(
            crm_map, source_id="device_crm", crosswalk=er_r2.crosswalk, target_object_type="BusinessParty", foreign_key_fields=fk_fields
        )
        erp_map_eff = inject_crosswalk_into_mapping(
            erp_map, source_id="device_erp", crosswalk=er_r2.crosswalk, target_object_type="BusinessParty", foreign_key_fields=fk_fields
        )

        crm_full_prof = scout_readonly_sqlite_profile(
            ref_dbs["crm_db"],
            include_tables=[em.source_table for em in crm_map_eff.entity_mappings],
            max_samples_per_table=0,
        )
        erp_full_prof = scout_readonly_sqlite_profile(
            ref_dbs["erp_db"],
            include_tables=[em.source_table for em in erp_map_eff.entity_mappings],
            max_samples_per_table=0,
        )

        crm_drv = DeclarativeSQLiteDriverBridge(ref_dbs["crm_db"], spec=compiled_spec, profile=crm_full_prof, adapter_mapping=crm_map_eff)
        erp_drv = DeclarativeSQLiteDriverBridge(ref_dbs["erp_db"], spec=compiled_spec, profile=erp_full_prof, adapter_mapping=erp_map_eff)
        bus = DeviceDriverRegistry(spec=compiled_spec)
        bus.mount(
            MountedDevice(
                device_id="device_crm",
                name="CRM",
                kind="sqlite",
                endpoint=f"sqlite://{ref_dbs['crm_db'].name}",
                bridge=crm_drv.as_source_bridge(),
                handled_actions=("win_crm_deal", "lose_crm_deal", "resolve_crm_ticket"),
                owned_object_types=("CrmAccount", "CrmDeal", "CrmTicket"),
                shared_object_types=("BusinessParty",),
            )
        )
        bus.mount(
            MountedDevice(
                device_id="device_erp",
                name="ERP",
                kind="sqlite",
                endpoint=f"sqlite://{ref_dbs['erp_db'].name}",
                bridge=erp_drv.as_source_bridge(),
                handled_actions=("confirm_o2c_order", "complete_o2c_delivery", "post_o2c_invoice", "record_o2c_payment"),
                owned_object_types=("AccountingEntity", "O2cCustomerProfile", "O2cOrder", "O2cDelivery", "O2cInvoice"),
                shared_object_types=("BusinessParty",),
            )
        )

        rt = OntologyRuntime(compiled_spec, ":memory:", world_mode="coupled", source_bridge=bus.as_source_bridge())
        try:
            crm_m0, _ = project_external_phenomenon(crm_full_prof, crm_map_eff)
            erp_m0, _ = project_external_phenomenon(erp_full_prof, erp_map_eff)
            merged = merge_projected_phenomena([crm_m0, erp_m0])
            admin = Actor(actor_id="adm", role="admin", clearance_tags={"*"})
            for item in merged:
                cls = compiled_spec.get_object_type(item["object_type"])
                rt.put_object(cls(**item["data"]), actor=admin)

            sales = Actor(actor_id="sales-1", role="sales_rep")
            sup = Actor(actor_id="sup-1", role="support_agent")

            hq_order_id = next(
                e["object_id"]
                for e in merged
                if e["object_type"] == "O2cOrder" and e["data"].get("order_number") == "SO-ERP-2002"
            )
            sub_order_id = next(
                e["object_id"]
                for e in merged
                if e["object_type"] == "O2cOrder" and e["data"].get("order_number") == "SO-ERP-2003"
            )
            urgent_tk_id = next(
                e["object_id"]
                for e in merged
                if e["object_type"] == "CrmTicket" and e["data"].get("priority") == "URGENT"
            )

            # Southstar HQ order (2002) MUST be blocked by Southstar HQ urgent CRM ticket
            try:
                rt.submit_action("LIVE-BLK-HQ", "confirm_o2c_order", {"order_id": hq_order_id}, sales)
                raise AssertionError("Expected OPEN_URGENT_CRM_TICKET_BLOCKS_ORDER on Southstar HQ order")
            except InvariantError as exc:
                assert exc.code == "OPEN_URGENT_CRM_TICKET_BLOCKS_ORDER"

            # Southstar Subsidiary order (2003, conflicting tax_id) MUST NOT be blocked and commits on ERP!
            res_sub = rt.submit_action("LIVE-OK-SUB", "confirm_o2c_order", {"order_id": sub_order_id}, sales)
            assert res_sub["status"] == "COMMITTED"

            # Resolve urgent CRM ticket -> Southstar HQ order (2002) unblocks and commits across coupled devices!
            res_tk = rt.submit_action(
                "LIVE-RES-TK",
                "resolve_crm_ticket",
                {"ticket_id": urgent_tk_id},
                sup,
            )
            assert res_tk["status"] == "COMMITTED"
            res_hq = rt.submit_action("LIVE-OK-HQ", "confirm_o2c_order", {"order_id": hq_order_id}, sales)
            assert res_hq["status"] == "COMMITTED"

            live_scenario_summary = {
                "hq_blocked_initially": True,
                "hq_blocked_error_code": "OPEN_URGENT_CRM_TICKET_BLOCKS_ORDER",
                "subsidiary_conflicting_tax_not_blocked_status": res_sub["status"],
                "ticket_resolved_status": res_tk["status"],
                "hq_confirmed_after_ticket_resolved_status": res_hq["status"],
                "crm_physical_writes": crm_drv.write_count,
                "erp_physical_writes": erp_drv.write_count,
            }
        finally:
            rt.close()
            crm_drv.close()
            erp_drv.close()

    return {
        "tamper_check_passed": True,
        "bottlecrm_source_untouched": True,
        "bottlecrm_source_sha256": bottlecrm_sha_before,
        "round_1_verify": round1,
        "round_2_verify_after_l4_work_card": round2,
        "live_coupled_bus_scenarios": live_scenario_summary,
    }


def run_live_agy_cli_fde_benchmark(
    *,
    models: tuple[str, ...] = ("gemini-4-neon-medium", "gemini-4-argon-medium", "gemini-3.8-flash-medium"),
    timeout_sec: int = 360,
    output_json_path: Path = Path("/tmp/wo11_agy_fde_live.json"),
) -> dict[str, Any]:
    """Run real isolated ``agy`` CLI FDE exploration + referee self-repair + outer anti-cheat audit."""
    cli_path = shutil.which("agy")
    if not cli_path:
        raise RuntimeError("agy CLI binary not found on PATH")

    ws_root = Path(tempfile.mkdtemp(prefix="wo11_agy_fde_ws_"))
    prep = prepare_isolated_player_workspace(ws_root)
    prompt = (
        "Read SKILL.md, requirements.md, and target_abi.json in your current directory. "
        "Explore data/crm_sample.sqlite3 and data/erp_sample.sqlite3 strictly read-only, "
        "write ONLY out/device_crm_mapping.json, out/device_erp_mapping.json, "
        "out/entity_resolution.json, and out/customer_overlay.json, "
        "then iterate with ./verify_driver.sh check and ./verify_driver.sh verify "
        "until verify reports ok=true and verdict=CORROBORATED with exit code 0. "
        "Do NOT attempt to modify read-only files. When finished, output a brief summary JSON."
    )

    attempts: list[dict[str, Any]] = []
    chosen_model: str | None = None
    agy_envelope: dict[str, Any] | None = None

    for model_name in models:
        cmd = [
            cli_path,
            "--model",
            model_name,
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--print-timeout",
            f"{timeout_sec}s",
            "--print",
            prompt,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(ws_root),
            text=True,
            capture_output=True,
            timeout=timeout_sec + 45,
        )
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        parsed_env: dict[str, Any] | None = None
        if stdout:
            try:
                parsed_env = json.loads(stdout[stdout.find("{") : stdout.rfind("}") + 1])
            except Exception:
                parsed_env = None

        out_complete = all(
            (ws_root / "out" / fn).is_file()
            for fn in (
                "device_crm_mapping.json",
                "device_erp_mapping.json",
                "entity_resolution.json",
                "customer_overlay.json",
            )
        )
        referee_probe = None
        if out_complete:
            # Check quickly whether player's out/*.json passes verify right now
            probe_proc = subprocess.run(
                ["./verify_driver.sh", "verify"],
                cwd=str(ws_root),
                text=True,
                capture_output=True,
            )
            try:
                referee_probe = json.loads(probe_proc.stdout)
            except Exception:
                referee_probe = {"returncode": probe_proc.returncode, "raw_stdout": probe_proc.stdout[:500]}

        attempts.append(
            {
                "model": model_name,
                "returncode": proc.returncode,
                "agy_status": parsed_env.get("status") if parsed_env else None,
                "num_turns": parsed_env.get("num_turns") if parsed_env else None,
                "duration_seconds": parsed_env.get("duration_seconds") if parsed_env else None,
                "usage": parsed_env.get("usage") if parsed_env else None,
                "conversation_id": parsed_env.get("conversation_id") if parsed_env else None,
                "stderr_tail": stderr[-400:],
                "out_complete": out_complete,
                "verify_probe_ok": bool(referee_probe and referee_probe.get("ok") is True),
            }
        )

        # If model hit rate limit/error before completing, try next available model
        if out_complete and referee_probe and referee_probe.get("ok") is True:
            chosen_model = model_name
            agy_envelope = parsed_env
            break

        # If output exists and we got a conversation_id, give agy 1 continuation turn to repair verify counterexamples!
        if parsed_env and parsed_env.get("conversation_id") and (proc.returncode == 0):
            conv_id = str(parsed_env["conversation_id"])
            repair_prompt = (
                "Run ./verify_driver.sh verify now, inspect every error or counterexample, "
                "repair out/*.json until ./verify_driver.sh verify exits 0 with ok=true and CORROBORATED."
            )
            proc2 = subprocess.run(
                [
                    cli_path,
                    "--model",
                    model_name,
                    "--conversation",
                    conv_id,
                    "--dangerously-skip-permissions",
                    "--disable-slash-commands",
                    "--output-format",
                    "json",
                    "--print-timeout",
                    f"{timeout_sec}s",
                    "--print",
                    repair_prompt,
                ],
                cwd=str(ws_root),
                text=True,
                capture_output=True,
                timeout=timeout_sec + 45,
            )
            stdout2 = (proc2.stdout or "").strip()
            try:
                parsed2 = json.loads(stdout2[stdout2.find("{") : stdout2.rfind("}") + 1])
            except Exception:
                parsed2 = None
            out_complete2 = all(
                (ws_root / "out" / fn).is_file()
                for fn in (
                    "device_crm_mapping.json",
                    "device_erp_mapping.json",
                    "entity_resolution.json",
                    "customer_overlay.json",
                )
            )
            probe2 = None
            if out_complete2:
                pp2 = subprocess.run(["./verify_driver.sh", "verify"], cwd=str(ws_root), text=True, capture_output=True)
                try:
                    probe2 = json.loads(pp2.stdout)
                except Exception:
                    probe2 = None
            attempts.append(
                {
                    "model": model_name,
                    "repair_turn": True,
                    "returncode": proc2.returncode,
                    "agy_status": parsed2.get("status") if parsed2 else None,
                    "num_turns": parsed2.get("num_turns") if parsed2 else None,
                    "duration_seconds": parsed2.get("duration_seconds") if parsed2 else None,
                    "usage": parsed2.get("usage") if parsed2 else None,
                    "conversation_id": conv_id,
                    "out_complete": out_complete2,
                    "verify_probe_ok": bool(probe2 and probe2.get("ok") is True),
                }
            )
            if out_complete2 and probe2 and probe2.get("ok") is True:
                chosen_model = model_name
                agy_envelope = parsed2
                break

    if chosen_model is None:
        report = {
            "ok": False,
            "workspace": str(ws_root),
            "attempts": attempts,
            "error": "No model produced verified out/*.json artifacts",
        }
        output_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
        raise RuntimeError(json.dumps(report, ensure_ascii=False, indent=2))

    # Independent anti-cheat referee audit on fresh untouched clones
    referee_audit = _independent_referee_audit(
        player_ws=ws_root,
        readonly_hashes_before=prep["readonly_hashes"],
        bottlecrm_sha_before=prep["bottlecrm_source_sha256"],
    )

    player_artifacts = {
        fn: json.loads((ws_root / "out" / fn).read_text("utf-8"))
        for fn in (
            "device_crm_mapping.json",
            "device_erp_mapping.json",
            "entity_resolution.json",
            "customer_overlay.json",
        )
    }

    report = {
        "ok": True,
        "chosen_model": chosen_model,
        "player_workspace": str(ws_root),
        "attempts": attempts,
        "agy_envelope": agy_envelope,
        "player_artifacts": player_artifacts,
        "independent_referee_audit": referee_audit,
    }
    output_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated live agy CLI AI-FDE benchmark")
    parser.add_argument("--model", default=None, help="Single agy model to try first")
    parser.add_argument("--timeout-sec", type=int, default=360)
    parser.add_argument("--output", default="/tmp/wo11_agy_fde_live.json")
    args = parser.parse_args()

    models = (
        (args.model, "gemini-4-argon-medium", "gemini-3.8-flash-medium")
        if args.model
        else ("gemini-4-neon-medium", "gemini-4-argon-medium", "gemini-3.8-flash-medium")
    )
    res = run_live_agy_cli_fde_benchmark(
        models=models,
        timeout_sec=args.timeout_sec,
        output_json_path=Path(args.output),
    )
    print(json.dumps(
        {
            "ok": res["ok"],
            "chosen_model": res["chosen_model"],
            "output_path": args.output,
            "round_1_verdict": res["independent_referee_audit"]["round_1_verify"]["verdict"],
            "round_1_action_cov": res["independent_referee_audit"]["round_1_verify"]["falsification"]["action_coverage_ratio"],
            "round_1_transition_cov": res["independent_referee_audit"]["round_1_verify"]["falsification"]["transition_coverage_ratio"],
            "round_2_ambiguous_after_work_card": len(res["independent_referee_audit"]["round_2_verify_after_l4_work_card"]["resolution_summary"]["ambiguous_candidates"]),
            "live_coupled_bus_scenarios": res["independent_referee_audit"]["live_coupled_bus_scenarios"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
