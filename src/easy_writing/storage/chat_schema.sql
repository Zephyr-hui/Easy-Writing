CREATE TABLE sessions (
    library_id TEXT NOT NULL,
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (library_id,id)
);
CREATE INDEX sessions_project ON sessions(library_id,project_id,created_at);
CREATE TABLE turns (
    seq INTEGER PRIMARY KEY,
    library_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(library_id,id),
    FOREIGN KEY(library_id,session_id) REFERENCES sessions(library_id,id)
);
CREATE INDEX turns_session ON turns(library_id,session_id,seq);
CREATE TABLE turn_memory (
    turn_seq INTEGER PRIMARY KEY REFERENCES turns(seq),
    scope_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    local_json TEXT NOT NULL,
    semantic_json TEXT,
    summary_status TEXT NOT NULL CHECK(summary_status IN ('local','pending','running','succeeded','failed'))
);
CREATE INDEX turn_memory_scope ON turn_memory(scope_key,topic,turn_seq);
CREATE TABLE rolling_memory (
    library_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    PRIMARY KEY(library_id,session_id,scope_key,topic),
    FOREIGN KEY(library_id,session_id) REFERENCES sessions(library_id,id)
);
CREATE TABLE summary_attempts (
    id TEXT PRIMARY KEY,
    turn_seq INTEGER NOT NULL REFERENCES turn_memory(turn_seq),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
    usage_json TEXT
);
