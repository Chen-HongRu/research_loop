"""Authority-transfer metadata, separate from research publication and STOP."""

MIGRATION_SCHEMA = """
CREATE TABLE migration_imports (
 id TEXT PRIMARY KEY, snapshot_ref TEXT NOT NULL, translation_ref TEXT NOT NULL,
 fence_ref TEXT NOT NULL, deployment_ref TEXT NOT NULL,
 source_identity_json TEXT NOT NULL, original_counters_json TEXT NOT NULL,
 blockers_json TEXT NOT NULL, started_at REAL NOT NULL, committed_at REAL,
 revision INTEGER NOT NULL DEFAULT 1, activation_ref TEXT,
 additional_elapsed_seconds REAL NOT NULL DEFAULT 0,
 context_ref TEXT NOT NULL, context_identity_json TEXT NOT NULL
);
CREATE TABLE migration_legacy_records (
 import_id TEXT NOT NULL REFERENCES migration_imports(id), kind TEXT NOT NULL,
 legacy_id TEXT NOT NULL, source_ref TEXT NOT NULL, native_status TEXT,
 PRIMARY KEY(import_id,kind,legacy_id)
);
CREATE TABLE migration_resolutions (
 import_id TEXT NOT NULL REFERENCES migration_imports(id), revision INTEGER NOT NULL,
 evidence_ref TEXT NOT NULL, intent_json TEXT NOT NULL,
 PRIMARY KEY(import_id,revision)
);
CREATE TABLE migration_phase_bindings (
 attempt_id TEXT PRIMARY KEY REFERENCES attempts(id), import_id TEXT NOT NULL REFERENCES migration_imports(id),
 binding_ref TEXT NOT NULL, intent_json TEXT NOT NULL
);
"""
