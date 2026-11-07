-- Compatible with the original backend database; no destructive migration.
CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, name TEXT NOT NULL, group_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id), external_id TEXT NOT NULL, language TEXT NOT NULL, channel TEXT NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(dataset_id, external_id));
CREATE INDEX IF NOT EXISTS conversation_dataset ON conversations(dataset_id, ordinal);
CREATE TABLE IF NOT EXISTS checklists(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checklist_versions(checklist_id TEXT NOT NULL REFERENCES checklists(id), version INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(checklist_id, version));
CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), checklist_id TEXT NOT NULL, checklist_version INTEGER NOT NULL, origin TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL, FOREIGN KEY(checklist_id, checklist_version) REFERENCES checklist_versions(checklist_id, version));
CREATE INDEX IF NOT EXISTS evaluation_conversation ON evaluations(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES evaluations(id), criterion_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_appeal ON appeals(evaluation_id, criterion_id) WHERE status='open';

-- Additive migration: old evaluation JSON and tables are unchanged.
CREATE TABLE IF NOT EXISTS execution_owners(
    id TEXT PRIMARY KEY, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_batches(
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, checklist_id TEXT NOT NULL,
    checklist_version INTEGER NOT NULL,
    FOREIGN KEY(checklist_id, checklist_version) REFERENCES checklist_versions(checklist_id, version)
);
CREATE INDEX IF NOT EXISTS batches_created ON evaluation_batches(created_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS batch_creation_requests(
    key_hash TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    batch_id TEXT NOT NULL UNIQUE REFERENCES evaluation_batches(id), response TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items(
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES evaluation_batches(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id), ordinal INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','interrupted')),
    owner_id TEXT REFERENCES execution_owners(id), evaluation_id TEXT REFERENCES evaluations(id),
    error_code TEXT, UNIQUE(batch_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS pending_batch_items ON batch_items(status, batch_id, ordinal);
CREATE TABLE IF NOT EXISTS evaluation_executions(
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES execution_owners(id),
    scope TEXT NOT NULL, key_hash TEXT, fingerprint TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    item_id TEXT REFERENCES batch_items(id),
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
    evaluation_id TEXT REFERENCES evaluations(id), response TEXT,
    error_code TEXT, duration_ms INTEGER, usage TEXT,
    created_at TEXT NOT NULL, provider_not_called INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scope, key_hash)
);
CREATE INDEX IF NOT EXISTS execution_owner_status ON evaluation_executions(owner_id, status);
CREATE INDEX IF NOT EXISTS execution_evaluation ON evaluation_executions(evaluation_id);
CREATE INDEX IF NOT EXISTS execution_batch_item ON evaluation_executions(item_id);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_item_execution ON evaluation_executions(item_id)
WHERE status IN ('running','succeeded');
CREATE TABLE IF NOT EXISTS batch_retry_state(
    item_id TEXT PRIMARY KEY REFERENCES batch_items(id),
    retry_at REAL, retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS provider_limits(
    model TEXT PRIMARY KEY, next_at REAL NOT NULL DEFAULT 0,
    blocked_until REAL NOT NULL DEFAULT 0, quota INTEGER NOT NULL DEFAULT 0
);
