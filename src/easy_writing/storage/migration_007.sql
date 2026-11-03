CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX chat_sessions_project_idx ON chat_sessions(project_id, created_at);
CREATE TABLE chat_turns (
    seq INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
    task_id TEXT NOT NULL UNIQUE REFERENCES work_tasks(id),
    request_json TEXT NOT NULL
);
CREATE INDEX chat_turns_session_idx ON chat_turns(session_id, seq);
