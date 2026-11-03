CREATE TABLE chapter_drafts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    plan_id TEXT NOT NULL REFERENCES chapter_plans(id),
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
    attempt_id TEXT,
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT
);
CREATE INDEX chapter_drafts_project ON chapter_drafts(project_id, created_at DESC);
