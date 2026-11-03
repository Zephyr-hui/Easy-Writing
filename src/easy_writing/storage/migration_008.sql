CREATE TABLE knowledge_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    id TEXT NOT NULL UNIQUE
);
INSERT INTO knowledge_identity VALUES(1, lower(hex(randomblob(16))));
