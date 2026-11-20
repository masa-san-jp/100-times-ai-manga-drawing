CREATE TABLE character_reference_labels(
  source_revision_id TEXT NOT NULL REFERENCES reference_revisions(id),
  original_hash TEXT NOT NULL REFERENCES assets(sha256),
  asset_hash TEXT NOT NULL REFERENCES assets(sha256),
  created_at TEXT NOT NULL,
  PRIMARY KEY(source_revision_id,original_hash)
);
