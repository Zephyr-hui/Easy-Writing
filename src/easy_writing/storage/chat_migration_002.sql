CREATE TABLE project_guidance (
    library_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    constraints_text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (library_id, project_id)
);
