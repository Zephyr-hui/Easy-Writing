CREATE TABLE embedding_models (
    fingerprint TEXT PRIMARY KEY, model_name TEXT NOT NULL, revision TEXT NOT NULL,
    dimension INTEGER NOT NULL, max_tokens INTEGER NOT NULL, policy_json TEXT NOT NULL
);
CREATE TABLE vector_segments (
    project_id TEXT NOT NULL REFERENCES projects(id), record_type TEXT NOT NULL, record_id TEXT NOT NULL,
    generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id), source_hash TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL REFERENCES embedding_models(fingerprint),
    segment_no INTEGER NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
    token_count INTEGER NOT NULL, vector BLOB NOT NULL,
    PRIMARY KEY(project_id,record_type,record_id,source_hash,model_fingerprint,segment_no)
);
CREATE INDEX vector_scope_idx ON vector_segments(project_id,model_fingerprint,source_hash);
CREATE INDEX entity_mentions_generation_idx ON entity_mentions(generation_id);
CREATE INDEX event_mentions_event_idx ON event_mentions(event_id);
CREATE INDEX evidence_source_idx ON evidence_links(source_chunk_id);
CREATE INDEX memory_generation_idx ON memory_items(generation_id);
