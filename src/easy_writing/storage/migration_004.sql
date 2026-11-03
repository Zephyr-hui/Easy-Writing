CREATE TABLE thread_transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL REFERENCES threads(id),
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL CHECK(to_state IN ('candidate','planted','reinforced','partially_resolved','resolved','cancelled','open')),
    event_id TEXT REFERENCES events(id),
    evidence_id TEXT REFERENCES evidence_links(id),
    correction INTEGER NOT NULL DEFAULT 0 CHECK(correction IN (0,1)),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX thread_transitions_thread_idx ON thread_transitions(thread_id,seq);
CREATE TABLE world_constraints (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    rule_fact_id TEXT NOT NULL REFERENCES facts(id),
    subject_id TEXT NOT NULL REFERENCES entities(id),
    predicate TEXT NOT NULL,
    operator TEXT NOT NULL CHECK(operator IN ('require','forbid')),
    value TEXT NOT NULL,
    from_event_id TEXT REFERENCES events(id),
    until_event_id TEXT REFERENCES events(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX world_constraints_project_idx ON world_constraints(project_id);
CREATE TABLE knowledge_uses (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    fact_id TEXT NOT NULL REFERENCES facts(id),
    actor_id TEXT NOT NULL REFERENCES entities(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    evidence_id TEXT NOT NULL REFERENCES evidence_links(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(fact_id,actor_id,event_id,evidence_id)
);
CREATE TABLE check_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    snapshot_id TEXT NOT NULL,
    through_chapter INTEGER,
    created_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE INDEX check_runs_project_idx ON check_runs(project_id,created_at);
CREATE TABLE check_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES check_runs(id),
    fingerprint TEXT NOT NULL,
    finding_json TEXT NOT NULL,
    UNIQUE(run_id,fingerprint)
);
CREATE TABLE finding_decisions (
    project_id TEXT NOT NULL REFERENCES projects(id),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','ignored','intentional','resolved')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,fingerprint)
);
CREATE TABLE finding_decision_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id),
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
