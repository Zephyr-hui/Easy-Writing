CREATE TABLE event_identity (
    event_id TEXT PRIMARY KEY REFERENCES events(id), target_id TEXT NOT NULL REFERENCES events(id),
    revealed_at_chapter INTEGER CHECK(revealed_at_chapter>0), CHECK(event_id<>target_id)
);
CREATE TABLE event_times (
    event_id TEXT PRIMARY KEY REFERENCES events(id), start_tick REAL, end_tick REAL,
    frame TEXT NOT NULL DEFAULT 'main', revealed_at_chapter INTEGER CHECK(revealed_at_chapter>0),
    CHECK(start_tick IS NULL OR end_tick IS NULL OR start_tick<=end_tick)
);
CREATE TABLE link_visibility (
    link_id TEXT PRIMARY KEY REFERENCES event_links(id), revealed_at_chapter INTEGER CHECK(revealed_at_chapter>0)
);
CREATE TABLE plot_threads (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
    description TEXT NOT NULL, UNIQUE(project_id,name)
);
CREATE TABLE event_thread_roles (
    plot_thread_id TEXT NOT NULL REFERENCES plot_threads(id), event_id TEXT NOT NULL REFERENCES events(id),
    role TEXT NOT NULL CHECK(role IN ('core','ordinary')), PRIMARY KEY(plot_thread_id,event_id)
);
CREATE TABLE fact_modes (
    fact_id TEXT PRIMARY KEY REFERENCES facts(id), mode TEXT NOT NULL CHECK(mode IN ('state','assertion'))
);
CREATE TABLE fact_awareness (
    fact_id TEXT NOT NULL REFERENCES facts(id), actor_id TEXT NOT NULL REFERENCES entities(id),
    learned_event_id TEXT NOT NULL REFERENCES events(id),
    revealed_at_chapter INTEGER CHECK(revealed_at_chapter>0), PRIMARY KEY(fact_id,actor_id)
);
CREATE TABLE graph_revisions (
    project_id TEXT PRIMARY KEY REFERENCES projects(id), revision INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX event_identity_target_idx ON event_identity(target_id);
CREATE INDEX event_roles_event_idx ON event_thread_roles(event_id);
CREATE INDEX fact_awareness_actor_idx ON fact_awareness(actor_id);
