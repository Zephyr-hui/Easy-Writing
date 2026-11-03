CREATE TABLE chapter_file_sources (
    version_id TEXT NOT NULL REFERENCES chapter_versions(id),
    file_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('txt','md','markdown','docx','pdf')),
    locations_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(version_id,file_hash,filename)
);
