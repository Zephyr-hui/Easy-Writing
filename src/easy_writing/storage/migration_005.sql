CREATE TABLE work_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL CHECK(kind IN ('ingest','research','plan')),
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX work_tasks_project_idx ON work_tasks(project_id,created_at);
CREATE TABLE chapter_plans (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL UNIQUE REFERENCES work_tasks(id),
    snapshot_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','adopted','written','rejected')),
    selected_route INTEGER,
    written_version_id TEXT REFERENCES chapter_versions(id),
    created_at TEXT NOT NULL
);
CREATE TABLE plan_decisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES chapter_plans(id),
    status TEXT NOT NULL,
    selected_route INTEGER,
    written_version_id TEXT REFERENCES chapter_versions(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
