CREATE TABLE revision_changes (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL, record_id TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX revision_changes_project_idx ON revision_changes(project_id,created_at);
CREATE TABLE record_dependencies (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL, reason TEXT NOT NULL,
    UNIQUE(project_id,source_type,source_id,target_type,target_id)
);
CREATE TABLE revision_reviews (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    change_id TEXT NOT NULL REFERENCES revision_changes(id),
    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','resolved')),
    reason TEXT NOT NULL, resolution TEXT,
    UNIQUE(change_id,source_type,source_id,target_type,target_id)
);
CREATE INDEX revision_reviews_target_idx ON revision_reviews(target_type,target_id,status);
CREATE TABLE record_successions (
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL, old_id TEXT NOT NULL, new_id TEXT NOT NULL,
    change_id TEXT NOT NULL REFERENCES revision_changes(id),
    PRIMARY KEY(kind,old_id,new_id)
);
CREATE TABLE ingestion_specs (
    job_id TEXT PRIMARY KEY REFERENCES ingestion_jobs(id), model_identity TEXT NOT NULL,
    max_chars INTEGER NOT NULL, configuration_json TEXT NOT NULL
);
CREATE TABLE ingestion_attempts (
    call_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ingestion_jobs(id), block_id TEXT NOT NULL,
    provider TEXT NOT NULL, attempts INTEGER NOT NULL, usage_json TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE derived_views (
    project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
    snapshot_id TEXT NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,name)
);
