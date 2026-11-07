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
