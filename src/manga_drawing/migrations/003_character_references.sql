ALTER TABLE projects ADD COLUMN purpose TEXT NOT NULL DEFAULT 'comic' CHECK(purpose IN ('comic','character_references'));
ALTER TABLE projects ADD COLUMN parent_project_id TEXT REFERENCES projects(id);
CREATE UNIQUE INDEX one_reference_project ON projects(parent_project_id) WHERE purpose='character_references';
CREATE TABLE character_reference_tasks(reference_id TEXT PRIMARY KEY REFERENCES reference_sets(id),page_id TEXT NOT NULL UNIQUE REFERENCES pages(id));
CREATE TABLE character_reference_selections(reference_id TEXT PRIMARY KEY REFERENCES reference_sets(id),source_revision_id TEXT NOT NULL REFERENCES reference_revisions(id),candidate_id TEXT REFERENCES candidates(id),asset_hash TEXT NOT NULL REFERENCES assets(sha256),created_at TEXT NOT NULL);
