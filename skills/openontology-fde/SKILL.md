---
name: openontology-fde
description: Autonomous Forward Deployed Engineer (FDE) workflow for inspecting unknown heterogeneous external databases, authoring declarative OpenOntology driver/entity-resolution/overlay artifacts in out/, and iterating against the read-only design referee CLI until CORROBORATED.
---

# OpenOntology FDE (Forward Deployed Engineer) Skill

You are acting as an **AI Forward Deployed Engineer (AI-FDE)** inside a physically isolated workspace. Your mission is to reverse-engineer heterogeneous external databases onto a locked OpenOntology Target ABI without modifying read-only source schemas, sample databases, standard ontologies, or referee scripts.

## 1. Strict Workspace & Anti-Cheat Rules

1. **Read-Only Zone (NEVER MODIFY)**:
   - `target_abi.json` (the locked Target Blueprint ABI)
   - `requirements.md` (business policy & entity alignment specification)
   - `data/*.sqlite3` (read-only sample databases)
   - `SKILL.md`
   - `./verify_driver.sh` (the official compiler + Popperian referee CLI)
2. **Writable Zone (`out/` ONLY)**:
   - Write ALL generated declarative JSON artifacts strictly inside `out/`:
     - `out/device_crm_mapping.json` (`AdapterMappingSpec`)
     - `out/device_erp_mapping.json` (`AdapterMappingSpec`)
     - `out/entity_resolution.json` (`EntityResolutionSpec`)
     - `out/customer_overlay.json` (`CustomerOverlaySpec`)
3. **Never Fake Safety or Hardcode Shortcuts**:
   - Inspect real tables, schemas, constraints, foreign keys, and sample rows with read-only SQLite/Python commands (`PRAGMA table_info`, `PRAGMA foreign_key_list`, `sqlite_master`). Ignore empty tables and unrelated legacy/noise tables or columns.
   - Preserve genuine gray-zone uncertainty in `entity_resolution.json` (keep `auto_merge_threshold` around `0.90` and `review_threshold` around `0.55`; NEVER lower thresholds to blindly force ambiguous entities into auto-merging).

---

## 2. Schema Contracts for Declarative Artifacts

### A. `AdapterMappingSpec` (`out/device_*_mapping.json`)
Top-level fields (`extra="forbid"`):
- `mapping_id`: string
- `source_kind`: string (e.g. `"sqlite"`)
- `treat_false_as_null`: bool
- `entity_mappings`: list of `EntityMappingSpec`:
  - `source_table`: physical SQLite table name
  - `target_object_type`: exact `name` in `target_abi.json -> object_types`
  - `row_filters`: optional list of `{"field": col, "op": "eq|ne|in|truthy|gt", "value": ...}`
  - `id_transform`: `FieldTransformSpec` targeting the object's exact `primary_key` (`target_field`, `source_field`, `kind="id_prefix"`, `prefix="<PREFIX>-"`). Note: when one table projects both a shared master type and a system-specific type, keep distinct `prefix` values if needed; the entity resolution engine will inject Crosswalk overrides onto shared master IDs and declared relation FKs.
  - `field_transforms`: list of `FieldTransformSpec`:
    - `direct`: `{target_field, source_field, kind: "direct", default: ...}`
    - `foreign_key_id`: `{target_field, source_field, kind: "foreign_key_id", prefix: "<PARENT_PREFIX>-"}` (or `lookup_join` when joining another entity projected from the same database/table: `{target_field, source_field, kind: "lookup_join", lookup_table, lookup_key_field, lookup_target_type}`)
    - `enum_map`: `{target_field, source_field, kind: "enum_map", default: ..., enum_map: {"<RAW_TOKEN>": "<CANONICAL_STATE>"}}` — MUST include **BOTH** currently observed raw tokens **AND** post-action target states declared in `target_abi.json` transitions so bidirectional actuation and physical readback succeed!
    - `scale_int` / `const` / `coalesce`
- `action_bindings`: list of `ActionExecutionBinding` covering every required source action on this device:
  - `action_name`: exact action name in `target_abi.json -> actions`
  - `external_table`: physical table updated by the action
  - `external_method`: SQL column assignment hint (e.g. `"stage=CLOSED_WON"` or `"ord_stage=erp_confirmed"`)
  - `target_object_type`: primary mutated object type of that action
  - `id_param`: exact primary ID parameter name on the action (e.g. `"deal_id"`, `"order_id"`, `"ticket_id"`, `"delivery_id"`, `"invoice_id"`)
  - `id_prefix`: MUST match `id_transform.prefix` of `target_object_type` on this device
  - `expected_state_field`: canonical status/stage field name on `target_object_type` (e.g. `"status"`, `"stage"`)
  - `expected_state_after`: canonical post-action state token declared in `target_abi.json` (NEVER the raw DB token!)

### B. `EntityResolutionSpec` (`out/entity_resolution.json`)
Top-level fields (`extra="forbid"`):
- `target_object_type`: exact shared object type name (check `target_abi.json`)
- `canonical_id_prefix`: e.g. `"PTY-"`
- `hard_key_fields`: exact canonical field(s) used for L1 exact match & hard conflict blocking
- `quasi_key_fields`: canonical name field(s) for L2 similarity
- `neighbor_match_fields`: auxiliary attribute field(s) for L3 corroboration
- `strip_tokens`: caller-supplied corporate suffixes/noise tokens (e.g. `["Co., Ltd.", "Ltd.", "Inc.", "Corp.", "有限公司", "有限责任公司", "股份有限公司"]`)
- `auto_merge_threshold`: `0.90`
- `review_threshold`: `0.55`
- `quasi_key_weight`: `0.70`
- `neighbor_weight`: `0.30`
- `survivorship`: `{ "<field>": ["<preferred_device_id>", "<fallback_device_id>"], ... }`

### C. `CustomerOverlaySpec` (`out/customer_overlay.json`)
Monotonic additive rules layered atop `target_abi.json` (`extra="forbid"`):
- `overlay_id`: string
- `base_model_id`: MUST match `target_abi.json -> model_id`
- `add_roles`: list of new custom roles (ONLY if extra roles are required; NEVER add typos)
- `add_action_preconditions`: `{ "<action_name>": [ InvariantRuleSpec, ... ] }`
  - Each `InvariantRuleSpec` has `name`, `code`, `target_type`, `id_param`, optional `when`, required `must_hold`, optional `description`.
  - Use safe zero-exec predicate AST:
    - `and` / `or` (`{"op": "and", "args": [...]}`), `not` (`{"op": "not", "arg": {...}}`)
    - Comparisons (`eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`, `not_in` with `left`, `right`)
    - Quantifiers (`exists`, `not_exists`, `count_eq`, `count_ge`, `count_le` with `object_type`, `where`)
    - Aggregates (`sum`, `min`, `max` with `object_type`, `field`, `where`, `default`)
    - Refs: `{"ref": "obj.<field>"}`, `{"ref": "obj.<rel>.<field>"}`, `{"ref": "target.<field>"}`, `{"ref": "target.<rel>.<field>"}`, `{"ref": "actor.role"}`, `{"ref": "param.<field>"}`
    - Literals: `{"lit": <value_or_list>}`
  - Every referenced field, multi-hop relation, action parameter, and literal `actor.role` value MUST exist in `target_abi.json`!
- NEVER emit destructive keys (`remove_*`, `alter_fields` weakening constraints, `update_primary_keys`) or expand `allowed_roles` on standard actions.

---

## 3. Closed-Loop Compilation & Falsification Workflow

1. **Explore**: Inspect `target_abi.json`, `requirements.md`, and run read-only SQLite discovery scripts against `data/*.sqlite3` (`sqlite_master`, `PRAGMA table_info`, `PRAGMA foreign_key_list`, distinct status/enum values, sample rows).
2. **Draft**: Write `out/device_crm_mapping.json`, `out/device_erp_mapping.json`, `out/entity_resolution.json`, and `out/customer_overlay.json`.
3. **Compile (`javac`)**: Run `./verify_driver.sh check`.
   - Fix any `UNKNOWN_ROLE`, `UNKNOWN_RELATION_IN_REF`, `UNKNOWN_FIELD_IN_REF`, `MISSING_REVERSE_ENUM_TARGET`, `INVALID_FSM_STATE_PROJECTION`, `ACTION_ID_PREFIX_MISMATCH`, or `OVERLAY_BREAKS_STANDARD_CONTRACT` diagnostics.
4. **Falsify (`JUnit` + Coupled Sandbox)**: Run `./verify_driver.sh verify`.
   - Inspect `verdict`, `resolution_summary`, `crucible_feedback`, and `falsification.counterexamples`.
   - Fix any `DIFFERENTIAL_STATE_MISMATCH`, `ADVERSARIAL_ESCAPE`, `DIRTY_EXTERNAL_WRITE`, `CROSS_DEVICE_WRITE_FORBIDDEN`, or `INSUFFICIENT_COVERAGE` errors and re-run until:
     - `ok == true`
     - `verdict == "CORROBORATED"` (exit code `0`)
     - No `CONFLICTING_HARD_KEY` pair was merged, and genuine L4 gray-zone pairs remain visible in `resolution_summary.ambiguous_candidates` for steward adjudication.

---

## 4. Imperative Thin Python Protocol Driver (`out/driver.py`)

When an external system speaks a network RPC / HTTP / stateful wizard protocol (rather than plain single-table SQLite updates), keep semantic rules in declarative JSON (`out/*_mapping.json`) and implement a minimal **Thin Protocol Transport Layer** in `out/driver.py` alongside `out/*_mapping.json`:

1. **Required Entrypoint Signature**:
   - `out/driver.py` MUST expose:
     ```python
     def build_device_bridge(
         endpoint_config: Mapping[str, Any],
         mapping: AdapterMappingSpec,
     ) -> SourceBridge:
     ```
   - Read connection URLs, database/tenant names, and credentials **exclusively** from `endpoint_config` at runtime.
   - Return `SourceBridge(actuate=actuate, observe=observe)`.
2. **AST Anti-Cheat & Security Cage (Enforced Statically Before Execution)**:
   - **Line Budget**: Keep `out/driver.py` concise (`<= 350` lines).
   - **Zero Hardcoding (`FORBIDDEN_HARDCODED_SAMPLE`)**: NEVER hardcode URLs, database names, usernames/passwords, sample record IDs, document numbers, partner names, or sample amounts into string literals. The outer referee injects an unseen randomized environment into `build_device_bridge(...)` after you finish!
   - **Allowed Imports Only (`FORBIDDEN_DRIVER_IMPORT`)**:
     - Stdlib transport & utilities: `urllib.request`, `urllib.error`, `urllib.parse`, `http.client`, `xmlrpc.client`, `json`, `sqlite3`, `typing`, `collections`, `dataclasses`, `copy`, `math`, `decimal`, `re`, `datetime`, `time`, `functools`, `itertools`, `uuid`, `hashlib`, `base64`, `__future__`.
     - OpenOntology SDK: `open_ontology.settlement` (`SourceBridge`), `open_ontology.spec`, `open_ontology.drivers`, and `open_ontology.induction.phenomenon` / `open_ontology.induction.dsl` (or allowed mapping dataclasses).
     - Forbidden: `subprocess`, `os`, `sys`, `pathlib`, `shutil`, `ctypes`, `socket`, `importlib`, `pytest`, test packages, `open_ontology.design`, `open_ontology.induction.falsifier`, `open_ontology.induction.crucible`.
   - **No Dynamic Execution or Disk Writes**: NEVER call `eval`, `exec`, `compile`, `__import__`, `open(..., "w"/"a"/"x"/"+")`, `.write_text`, `.unlink`, etc.
3. **Live Physical Ground-Truth Readback (`observe`)**:
   - `observe(move_envelope=None)` MUST return `dict[str, list[dict[str, Any]]]` keyed by exact `ObjectType` names from `target_abi.json`, with canonical IDs derived using `mapping` prefixes/rules and values matching ABI field types and FSM transition keys.
   - NEVER echo expected post-states from `move_envelope["object_diff"]`; always query live external state over RPC.
   - When real ERP/CRM databases contain unrelated standalone/orphan/cancelled documents (e.g. unlinked stock transfers, standalone accounting entries with missing origin document, or cancelled drafts/orders that have no valid parent), handle/filter non-O2C noise consistently without fabricating missing parent links or emitting invalid schemas.
4. **Actuation & Out-of-Band Drift Detection (`actuate` -> `DEVICE_STATE_MISMATCH`)**:
   - `actuate(move_envelope)` reads `move_envelope["action_type"]`, `move_envelope.get("params")`, and `move_envelope.get("object_diff")`, performs the corresponding live external RPC mutations, and refreshes/commits external state cleanly (mapping all downstream side effects such as stock move quantities or payment wizards).
   - **Critical State-Drift Contract**: If the external server returns an expected business-state conflict / RPC application fault because the target record was modified out-of-band (for example, cancelled or already settled outside OpenOntology), catch that state-conflict RPC fault cleanly in `actuate` rather than crashing mid-Phase-B, so Phase B's immediate `observe()` reports true physical state back to `HomomorphicSettlementAspect`, triggering `DeviceStateMismatchError` (`DEVICE_STATE_MISMATCH`) with zero ABox corruption.
