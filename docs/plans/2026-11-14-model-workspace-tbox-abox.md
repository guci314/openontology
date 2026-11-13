# Model / Workspace Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Coding and testing tasks run through `BoardRunner` and the CodeBuddy developer/tester route; the architect reviews evidence and diffs. Default checkpoint: complete cards 01–03, then report for feedback before 04–06.

**Goal:** Distinguish reusable domain definitions, isolated workspaces, and addressable business objects without losing data or breaking existing APIs.

**Architecture:** A model consists of a structural TBox projection and a behavior projection. A workspace binds a model to fact state (ABox), a runtime, and execution history. These are logical boundaries, not new business tables or OWL services; executable model definitions remain Python code.

**Tech Stack:** Python, Pydantic 2, SQLite, pytest, existing Playwright presentation tests.

## Safety and environment

- Implementation worktree: `/Users/guci/.local/share/opencode/worktree/2341b0/model-workspace`.
- Original working copy: `/Users/guci/work/open-ontology`; it contains substantial user changes. Never reset, clean, commit, or overwrite those changes.
- The worktree includes a byte-for-byte snapshot of the original source (including uncommitted source), but NOT its live `.instances` directory.
- Existing ignored test resources (the BottleCRM wheel/source/fixture backup and generated `video/` media) were copied separately for full-suite verification. The first environment-only run reported 398 passed, 6 failed, 12 errors, 6 skipped because those resources were initially absent; no feature implementation had started. Do not weaken or skip their tests to hide a missing fixture.
- After provisioning those existing resources, the unchanged-source baseline passed: **420 passed, 2 skipped in 90.72s** using `/Users/guci/miniforge3/bin/python -m pytest -q` in the implementation worktree. The two skips require explicitly enabled live-host credentials/quota.
- Pre-change SQLite backup and source fingerprints: `/private/var/folders/nj/9yhn6dcd1xl3c7v6zs7fdqn40000gn/T/opencode/model-workspace-7idw3hrc`.
- Production board database stays at `/Users/guci/work/open-ontology/.instances/board_prod.sqlite3`. Only the architect's explicit board actions may update it. Workers must not open, reset, or test against it.
- `schema.sql`, `__init__.py`, and `ui_display.json` remain untouched. The user's subsequent aggregate-root requirement permits a narrowly scoped generic type-metadata change in `spec.py` and its registry serialization in `runtime.py`; no domain-specific kernel logic or runtime line-budget exemption is permitted. Do not rename the Python package.
- No commits or pushes without an explicit request. Do not start/restart the production demo service.

## Terminology and contracts

```text
Model task_board
  TBox: object types, fields, interfaces, link types, declared constraints
  Behavior: actions, functions, automations and their declared contracts
Workspace board-prod
  model_id: task_board
  runtime_id: rt-board-prod
  ABox: object values and relationship assertions
  history: action/audit records (not TBox or ABox declarations)
Object address: board-prod / Board / BD-CC-001
```

- `OntologySpec` is the existing executable model declaration registry. Keep it compatible rather than introducing a parallel registry.
- `instance_id` aliases `workspace_id`; `ontology_id` aliases `model_id`. Keep one backing registry and one value for each identity.
- Filesystem `workspace_root` currently means a code working directory, NOT a business workspace ID. Document this distinction; do not change directory behavior opportunistically.
- TBox/ABox terminology is borrowed for structural/fact separation, not a claim of OWL entailment or serialization of arbitrary Python validators.
- Model version management, RDF/OWL conversion, multi-model runtimes, new UI pillars, and automatic schema migration are out of scope.

### User steering: aggregate-root declarations and workspace listing

The user explicitly requested that ObjectType declare whether it is an aggregate root and that "list all workspaces" include every workspace and its aggregate-root objects.

- Add `__aggregate_root__: ClassVar[bool] = False` as TYPE metadata, not an instance field or database column. Explicitly declare `Board` a root in its own domain module. Do not infer roots from graph degree, ownership flags, type names, or the absence of incoming links; other models with no declaration are reported as such.
- Preserve the flag in serialized type metadata and expose `aggregate_root` in the TBox projection. It must not appear in business-object JSON values or create a new table.
- Provide a read-only `CommandCenterRegistry.list_workspaces(actor)` listing all registered workspaces, including in-memory ones and workspaces with zero roots. Each row identifies `workspace_id`, `model_id`, declared root types, availability, and `aggregate_roots` entries with `object_type`, `object_id`, and role-filtered/masked `title`; full identity is workspace + type + ID.
- Root enumeration must use the existing runtime query/security path with the supplied actor, never raw SQL that bypasses RLS or field masking. Include all visible roots without an arbitrary result cap; normal child objects are not listed as roots.
- Distinguish "no declared root types", "declared root types but no visible root objects", and "unavailable/not loaded". An unreadable workspace is not an empty workspace. Catalog-only persistent entries must remain visible with explicit availability; opening them is addressed by C-WS-04, not by silently recreating state.
- This is declaration/navigation metadata only. It does not claim to enforce new aggregate transaction boundaries or containment constraints.
- Implement this as a small C-WS-02 refinement before the main catalog card, and integrate the projection in C-WS-03. UI/HTTP exposure remains C-WS-05 unless explicitly requested otherwise.

### User steering: a checkpoint and explicit goal assessment on EVERY completion

The user subsequently required: "每个工单完成后都应该更新state并且判断是否需要更新goal".

C-WS-STATE-01 is prioritized after the running root refinement and before the remaining main cards. Every kanban completed action must atomically complete its card/run, update Board.state, persist an explicit goal decision/reason, optionally update Board.goal when changed, and clear the unfolded counter. Missing or contradictory metadata refuses completion; no "done now, update memory later" shortcut. Old JSON remains readable; new completion writes follow the strict contract. Host/runner parsing and forwarding must support the fields, report rejected writes honestly, and allow corrected retries without duplicate claims/runs. No LLM belongs in a pure action.

The original running coordinator still uses old code during this transition. On accepting the current card the architect explicitly records its reviewed checkpoint and goal assessment using compatible Board updates/comments, then verifies the atomic gate in the worktree. Do not claim the production service has been upgraded or write a new incompatible CardRun format to it before reviewed deployment. Do not overwrite reviewed Board.goal/state from stale documents.

## Execution discipline (each card)

1. Read the specified existing implementation and neighboring tests.
2. Add one focused behavioral test; run it and preserve its expected failure before production edits.
3. Implement the smallest change; run that test to green.
4. Repeat for remaining acceptance cases, then run the specified regression suite.
5. Record exact commands, failures-to-green, numeric test results, and modified files in the card's evidence artifact.
6. Return schema-valid `HostResult` with real numeric evidence. Never claim tests were run when they were not.

## C-WS-01 — Non-destructive opening and isolated tests

**Files:**
- Modify `examples/command_center_demo.py`.
- Modify `open_ontology/agent_hosts.py` only in the database-takeover bootstrap path.
- Modify `tests/test_command_center.py`, `tests/test_command_center_presentation.py`, and `tests/test_agent_hosts.py`.
- A focused `tests/test_workspace_safety.py` may be added if this keeps persistence checks readable.

**Steps:**
1. Add a real SQLite test that creates a board, edits state through actions, closes it, and reopens the demo. Assert all original object values, versions, relationships, and action records survive, and no seed actions repeat. Demonstrate failure on the current unconditional reset.
2. Separate opening existing board data from creating fresh demo data. Default is non-destructive. Destructive reseeding requires an explicit `reset` request scoped to the specified database, never a broad directory deletion. Existing incompatible/partial databases must fail clearly rather than being overwritten or silently seeded.
3. Change both `build_default_command_center()` fixtures to pass a per-test `tmp_path` data directory. Check all other call sites for accidental use of the repository's `.instances` during tests.
4. Add a missing-object takeover test against an empty search directory: it must raise `LookupError` and leave no `.instances` directory, database, board, or seed action behind. Remove the default-demo creation fallback from takeover; ordinary lookup is not initialization.
5. Test explicit reset against a disposable test database only. Test first-time creation and repeated opening as separate cases.
6. Run targeted regressions and then the complete suite in this worktree.

**Verification:**
```bash
/Users/guci/miniforge3/bin/python -m pytest tests/test_command_center.py tests/test_command_center_presentation.py tests/test_agent_hosts.py -q
/Users/guci/miniforge3/bin/python -m pytest -q
```

**Acceptance:** no test touches the original repository's database; repeated opening preserves state; lookup has no bootstrapping side effect; existing APIs/tests keep their behavior except the intentionally removed destructive fallback.

**Independent-review follow-up (C-WS-01-R2):** readonly SQLite connections must use an escaped absolute file URI, including in the object-scanning helper, so literal `#`, `%xx`, or `?` in a directory cannot redirect a supposedly readonly connection or swallow its `mode=ro`. Add real reserved-path regressions and compare complete object/link/audit state across reopening, not just counts and selected audit fields.

## C-WS-02 — Persist workspace identity without new business tables

**Prerequisite refinement:** C-WS-02-R1 adds the user-approved aggregate-root metadata and read-only workspace/root listing (see above). The main C-WS-02 must preserve that listing and include any catalog-only descriptors with explicit `not_loaded` availability, not silently omit them or treat their roots as empty.

**Files:**
- Add `open_ontology/workspaces.py` and `tests/test_workspaces.py`.
- Modify `open_ontology/workshop.py` and `examples/command_center_demo.py`.
- Extend `tests/test_command_center.py` as necessary.

**Steps:**
1. Add tests for workspace descriptor validation, save/load across process-like reconstruction, relative-path resolution, idempotent identical registration, conflicting duplicate IDs, missing files, unavailable models, and ephemeral runtime handling. Observe the relevant test fail before adding each capability.
2. Add a small `WorkspaceDescriptor` with `workspace_id`, `model_id`, `runtime_id`, and `db_path`. Do not serialize runtime objects, actors, callables, credentials, or arbitrary import strings.
3. Add a catalog backed by `.instances/workspaces.json` with an explicit format version. Relative database paths resolve relative to the manifest. Saves use a temporary file and atomic replacement; malformed or unsupported manifests fail clearly without overwriting the original.
4. Resolve models using an explicitly provided trusted model registry. Never import arbitrary code named by a JSON file. Resolving a workspace must not create a missing database.
5. Expose `register_workspace` and `get_workspace` on `CommandCenterRegistry` and workspace/model identity properties on entries. Keep `register_instance`, `get_instance`, `instances`, and `instance_id` working against the SAME underlying entries, not copied registries. Preserve old constructor signatures.
6. Wire an optional workspace catalog into demo assembly. Register the existing `board-prod` against its actual `runtime.db_path`, not the cosmetic `db_uri`. Reopening does not rewrite facts or seed cards. Only file-backed runtimes are persisted; in-memory runtime entries remain ephemeral even if a display URI happens to resemble a file.
7. Keep HTTP payloads and UI labels unchanged in this card; their additive migration is C-WS-05.

**Review precautions:** loading a malformed/unsupported manifest must not overwrite it; an attempted conflicting registration must not leave a partially updated in-memory registry. Fresh registration may save metadata, but descriptor lookup and trusted model/path resolution are read-only and must not create missing directories or databases. Atomic replacement protects against partial files; do not claim unsupported multi-process writer transactions.

**Verification:**
```bash
/Users/guci/miniforge3/bin/python -m pytest tests/test_workspaces.py tests/test_command_center.py tests/test_agent_hosts.py -q
```

**Acceptance:** a new catalog reader can locate `board-prod` and validate its trusted model binding without constructing/resetting the demo; aliases reference the same objects; no new SQLite business tables or renamed existing IDs.

## C-WS-03 — TBox and behavior projections, one source of truth

**Files:**
- Add `open_ontology/model_views.py` and `tests/test_model_views.py`.
- Refactor `open_ontology/workshop.py` metadata projection helpers to reuse the new projections.

**Steps:**
1. Add failing tests for the proposed public projections `describe_tbox(spec)` and `describe_behavior(spec)` using a tiny local test model with types, constrained fields, an interface, a relationship, an action, a function, and an automation.
2. Implement `describe_tbox(spec)` as a read-only, JSON-serializable structural view of existing declarations, including each type's explicit `aggregate_root` flag. No database access, instance values, audit records, or handler source belongs in this output (declaration constants/defaults are fine). Describe only declaratively available constraints; do not pretend arbitrary validators can be serialized.
3. Implement `describe_behavior(spec)` for existing action/function/automation contracts. Retain action parameter schemas, role/approval declarations, and the preexisting metadata shape. Do not introduce a second execution or validation path.
4. Reuse these views from the existing `_spec_meta` implementation without breaking its response keys, field order assumptions, roles, labels, topology, masking metadata, or legacy endpoints. Workspace-derived default-actor roles stay outside the pure TBox view.
5. Add a real two-runtime test sharing the same model: mutating one fact graph must not affect the other; both model projections stay unchanged and must not expose the edited fact values. Add a fork isolation check where useful.
6. Run the complete suite; preserve red/green and final numeric evidence.

**Verification:**
```bash
/Users/guci/miniforge3/bin/python -m pytest tests/test_model_views.py tests/test_command_center.py tests/test_command_center_presentation.py tests/test_kernel_aspects.py -q
/Users/guci/miniforge3/bin/python -m pytest -q
```

**Acceptance:** reusable definitions and mutable facts are separated logically, not stored twice; existing generic UI and governance remain intact; no new dependencies or core exports.

## Checkpoint after C-WS-03

- Architect reviews per-card changes relative to the captured source baseline (not only Git HEAD, which lacks existing uncommitted features).
- Independently verify targeted/full tests and original board/data preservation.
- Report exact completed cards, actual test counts, worktree path, and what is still pending. Do not present batch one as the entire six-card change.
- Do not begin C-WS-04 through C-WS-06 until feedback authorizes the next batch.

## C-WS-04 — Explicit takeover addressing (next batch)

**Files:** `open_ontology/agent_hosts.py`, `open_ontology/workspaces.py`, `tests/test_agent_hosts.py`.

- Add `takeover_workspace(workspace_id)` using the catalog and trusted model resolution; generic opening must not import `kanban.spec`.
- Add board-scoped takeover with `workspace_id` plus `Board`/`Card` identity as a domain adapter. Keep the old object-ID entry point.
- Duplicate matches produce explicit candidates, never first-match selection. Missing workspaces/models/files fail without creating data.
- Test two databases with the same Board ID, workspace-only overview, a specific Board, Card-to-Board resolution, and read-only discovery.

## C-WS-05 — Additive API and presentation migration (next batch)

**Files:** `open_ontology/workshop.py`, `open_ontology/workshop_ui.py`, `tests/test_command_center.py`, `tests/test_command_center_presentation.py`.

- Labels become `Model · 领域模型`, `Workspace · 工作空间`, plus the existing Runtime/Device pillars. Do not add TBox/ABox top-level tabs.
- Model details distinguish TBox structure and behavior; workspace facts are labeled ABox. Preserve generic, domain-independent UI rendering.
- Add `workspace` routing and catalog `models`/`workspaces` aliases while retaining `instance`, `ontologies`, and `instances`. Conflicting new/old parameters must fail rather than silently select a workspace.
- Preserve old DOM IDs initially. Exercise all existing models/workspaces, sidebar exclusivity, masking, actions, approval, audit, simulation and fork isolation.

## C-WS-06 — Documentation, end-to-end acceptance, fold (next batch)

**Files:** add `GLOSSARY.md`; update `README.md`, `docs/ARCHITECTURE.md`, `docs/TASK_BOARD.md`, `_state.md`, `_execution.md`, plus optional multi-doc `Board.goal` assembly in `examples/command_center_demo.py` and `open_ontology/agent_hosts.py` (preserving single-file default compatibility for existing unit tests).

- Keep OMG MOF meta-level separation clean: `docs/ARCHITECTURE.md` stays pure M2 platform/kernel constitution, while `docs/TASK_BOARD.md` owns M1 `task_board` contracts and M0 `BD-CC-001` dogfooding / completion-checkpoint / SSoT rules.
- Remove incorrect activated/unactivated and four-fully-orthogonal language; distinguish workspace identity from business object identity.
- Verify reopening, catalog identity, explicit addressing, duplicate-ID handling, model/fact isolation, fork isolation, legacy compatibility and all governance guards.
- Verify the original data set and backup, and do not publish unrun historical test counts as current evidence.
- Only after successful review, sync actual documentation (`docs/ARCHITECTURE.md` + `docs/TASK_BOARD.md` for `BD-CC-001`) to `Board.goal`/`Board.state` and fold the completed cards.

## Post-Batch Follow-up: C-KERNEL-SEC-01 — M2 Microkernel Guardrail Hardening

Tracked after `C-WS-06` so `schema.sql` / `runtime.py` baselines remain frozen during the Model/Workspace migration:
1. Enforce `_check_authority` across changes staged inside `action.post_approval` in `open_ontology/runtime.py:_run_handler` (keeping `runtime.py <= 1400` lines).
2. Extend `ontology_write_gate` triggers in `open_ontology/schema.sql` to guard `BEFORE INSERT / UPDATE` on `ontology_action_runs` and `BEFORE INSERT / UPDATE / DELETE` on `ontology_registry`.
3. Audit HTTP/UI/enumeration call sites to ensure explicit caller `Actor` propagation (deny-by-default; never fall back to `role="system"` across external boundaries).

## Final gate

The user can say: "接管工作空间 board-prod，并以其中的看板 BD-CC-001 为工作入口。"

Old data remains; old calls work; new addressing never guesses; TBox/ABox are not duplicated; all reported numerical evidence comes from executed verification.
