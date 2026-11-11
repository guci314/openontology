-- OpenOntology L2 storage engine (SQLite) -- v3 pure-business ontology.
--
-- 5 pure ontology tables (registry, objects, links, action runs, overlay)
-- plus the physical write gate.  No transport, messaging or external-system
-- synchronisation tables live here: the only observable output of the ontology
-- is the pure-business audit ledger ``ontology_action_runs``.
--
-- The write gate row (id=1) is closed by default.  Every mutation of the
-- object graph must go through OntologyRuntime, which temporarily opens the
-- gate inside the very same transaction that performs the mutation.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ---------------------------------------------------------------------------
-- 0. Physical write gate
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ontology_write_gate (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    is_open INTEGER NOT NULL DEFAULT 0 CHECK (is_open IN (0, 1))
);

INSERT OR IGNORE INTO ontology_write_gate (id, is_open) VALUES (1, 0);

-- ---------------------------------------------------------------------------
-- 1. Declarative registry (object types, link types, functions, actions)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ontology_registry (
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    spec_json  TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);

-- ---------------------------------------------------------------------------
-- 2. Objects
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ontology_objects (
    object_type        TEXT    NOT NULL,
    object_id          TEXT    NOT NULL,
    title              TEXT,
    properties_json    TEXT    NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    security_tags_json TEXT    NOT NULL DEFAULT '[]',
    content_hash       TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (object_type, object_id)
);

CREATE INDEX IF NOT EXISTS idx_objects_type ON ontology_objects (object_type);
CREATE INDEX IF NOT EXISTS idx_objects_hash ON ontology_objects (content_hash);

-- ---------------------------------------------------------------------------
-- 3. Links
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ontology_links (
    link_type   TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    PRIMARY KEY (link_type, source_type, source_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_links_source
    ON ontology_links (link_type, source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_links_target
    ON ontology_links (link_type, target_type, target_id);

-- ---------------------------------------------------------------------------
-- 4. Action runs (append-only pure-business audit ledger)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ontology_action_runs (
    run_id            TEXT PRIMARY KEY,
    action_type       TEXT NOT NULL,
    actor_id          TEXT NOT NULL,
    actor_role        TEXT NOT NULL,
    params_json       TEXT NOT NULL,
    status            TEXT NOT NULL
                      CHECK (status IN ('PENDING_APPROVAL', 'COMMITTED', 'REVERTED', 'REJECTED')),
    read_versions_json TEXT NOT NULL DEFAULT '{}',
    object_diff_json  TEXT NOT NULL DEFAULT '{}',
    approved_by       TEXT,
    reverted_by_run_id TEXT,
    executed_at       TEXT NOT NULL,
    error_code        TEXT,
    error_message     TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON ontology_action_runs (status);
CREATE INDEX IF NOT EXISTS idx_runs_action ON ontology_action_runs (action_type);

-- ---------------------------------------------------------------------------
-- 5. Ontology overlay (field-level ownership patches over source-backed rows)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ontology_overlay (
    object_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    patch_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (object_type, object_id)
);

-- ---------------------------------------------------------------------------
-- 6. Physical governance triggers
-- ---------------------------------------------------------------------------
CREATE TRIGGER IF NOT EXISTS trg_objects_guard_insert
BEFORE INSERT ON ontology_objects
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct INSERT into ontology_objects is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_objects_guard_update
BEFORE UPDATE ON ontology_objects
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct UPDATE on ontology_objects is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_objects_guard_delete
BEFORE DELETE ON ontology_objects
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct DELETE from ontology_objects is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_links_guard_insert
BEFORE INSERT ON ontology_links
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct INSERT into ontology_links is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_links_guard_update
BEFORE UPDATE ON ontology_links
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct UPDATE on ontology_links is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_links_guard_delete
BEFORE DELETE ON ontology_links
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct DELETE from ontology_links is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_overlay_guard_insert
BEFORE INSERT ON ontology_overlay
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct INSERT into ontology_overlay is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_overlay_guard_update
BEFORE UPDATE ON ontology_overlay
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct UPDATE on ontology_overlay is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_overlay_guard_delete
BEFORE DELETE ON ontology_overlay
FOR EACH ROW
WHEN (SELECT is_open FROM ontology_write_gate WHERE id = 1) = 0
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: direct DELETE from ontology_overlay is blocked (write gate closed)');
END;

CREATE TRIGGER IF NOT EXISTS trg_action_runs_guard_delete
BEFORE DELETE ON ontology_action_runs
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'GOVERNANCE_VIOLATION: ontology_action_runs is append-only; DELETE is forbidden');
END;
