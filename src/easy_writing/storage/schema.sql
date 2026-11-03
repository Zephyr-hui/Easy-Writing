CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    chapter_no INTEGER NOT NULL CHECK(chapter_no>0), title TEXT NOT NULL,
    latest_version_id TEXT REFERENCES chapter_versions(id),
    published_generation_id TEXT REFERENCES ingestion_jobs(id),
    UNIQUE(project_id, chapter_no)
);
CREATE TABLE IF NOT EXISTS chapter_versions (
    id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL REFERENCES chapters(id),
    revision INTEGER NOT NULL, content_hash TEXT NOT NULL, content TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(chapter_id,content_hash), UNIQUE(chapter_id,revision)
);
CREATE TABLE IF NOT EXISTS source_chunks (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES chapter_versions(id),
    paragraph_id TEXT NOT NULL, scene_no INTEGER NOT NULL,
    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, text TEXT NOT NULL,
    CHECK(end_offset>start_offset), UNIQUE(version_id,paragraph_id)
);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES chapter_versions(id),
    signature TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','failed','published')),
    total_blocks INTEGER NOT NULL, completed_blocks INTEGER NOT NULL DEFAULT 0,
    error_code TEXT, owner TEXT, lease_until REAL, created_at TEXT NOT NULL,
    UNIQUE(version_id,signature)
);
CREATE TABLE IF NOT EXISTS extraction_blocks (
    job_id TEXT NOT NULL REFERENCES ingestion_jobs(id), block_id TEXT NOT NULL,
    result_json TEXT NOT NULL, call_id TEXT NOT NULL, PRIMARY KEY(job_id,block_id)
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL, kind TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'unconfirmed',
    merged_into TEXT REFERENCES entities(id), CHECK(merged_into IS NULL OR merged_into<>id)
);
CREATE TABLE IF NOT EXISTS entity_aliases (
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL, generation_id TEXT REFERENCES ingestion_jobs(id),
    author_confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entity_mentions (
    id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
    generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id)
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
    title TEXT NOT NULL, summary TEXT NOT NULL, story_time TEXT,
    review_status TEXT NOT NULL DEFAULT 'unconfirmed'
);
CREATE TABLE IF NOT EXISTS event_entities (
    event_id TEXT NOT NULL REFERENCES events(id), entity_id TEXT NOT NULL REFERENCES entities(id),
    PRIMARY KEY(event_id,entity_id)
);
CREATE TABLE IF NOT EXISTS event_mentions (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
    source_chunk_id TEXT NOT NULL REFERENCES source_chunks(id), mention_kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
    subject_id TEXT NOT NULL REFERENCES entities(id), predicate TEXT NOT NULL,
    object_value TEXT NOT NULL, object_entity_id TEXT REFERENCES entities(id),
    knowledge_holder_id TEXT REFERENCES entities(id),
    effective_from_event_id TEXT REFERENCES events(id), effective_until_event_id TEXT REFERENCES events(id),
    certainty TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'unconfirmed'
);
CREATE TABLE IF NOT EXISTS event_links (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
    from_event_id TEXT NOT NULL REFERENCES events(id), to_event_id TEXT NOT NULL REFERENCES events(id),
    relation_type TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'unconfirmed',
    UNIQUE(generation_id,from_event_id,to_event_id,relation_type)
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
    title TEXT NOT NULL, description TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'candidate', review_status TEXT NOT NULL DEFAULT 'unconfirmed'
);
CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL UNIQUE REFERENCES ingestion_jobs(id),
    text TEXT NOT NULL, is_stale INTEGER NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'unconfirmed'
);
CREATE TABLE IF NOT EXISTS evidence_links (
    id TEXT PRIMARY KEY, record_type TEXT NOT NULL, record_id TEXT NOT NULL,
    source_chunk_id TEXT NOT NULL REFERENCES source_chunks(id), quote TEXT NOT NULL,
    quote_start INTEGER NOT NULL, quote_end INTEGER NOT NULL, CHECK(quote_end>quote_start)
);
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
    record_type TEXT NOT NULL, record_id TEXT NOT NULL, text TEXT NOT NULL,
    UNIQUE(generation_id,record_type,record_id)
);
CREATE TABLE IF NOT EXISTS dependencies (
    generation_id TEXT NOT NULL REFERENCES ingestion_jobs(id),
    record_type TEXT NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(generation_id,record_type,record_id)
);
CREATE TABLE IF NOT EXISTS edit_history (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    record_type TEXT NOT NULL, record_id TEXT NOT NULL, before_json TEXT NOT NULL,
    after_json TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_version_idx ON source_chunks(version_id);
CREATE INDEX IF NOT EXISTS evidence_record_idx ON evidence_links(record_type,record_id);
CREATE INDEX IF NOT EXISTS entity_project_idx ON entities(project_id,name);
CREATE INDEX IF NOT EXISTS alias_lookup_idx ON entity_aliases(alias);
CREATE INDEX IF NOT EXISTS facts_generation_idx ON facts(generation_id);
CREATE INDEX IF NOT EXISTS events_generation_idx ON events(generation_id);
CREATE INDEX IF NOT EXISTS mentions_entity_idx ON entity_mentions(entity_id);
