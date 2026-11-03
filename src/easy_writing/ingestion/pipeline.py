import json
from collections.abc import Callable

from easy_writing.ingestion.chunker import split_chapter
from easy_writing.ingestion.extractor import PROMPT_VERSION, SYSTEM_PROMPT
from easy_writing.ingestion.models import ChapterExtraction, locate_evidence, validate_evidence
from easy_writing.llm.base import LLMError
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import Repository, digest, uid


class IngestionPipeline:
    def __init__(self, repository: Repository, client, *, model_identity: str, max_chars=2400, lease_seconds=900):
        self.repo = repository
        self.client = client
        self.model_identity = model_identity
        self.max_chars = max_chars
        self.lease_seconds = lease_seconds
        self.signature = digest(json.dumps({
            "prompt_version": PROMPT_VERSION, "system": SYSTEM_PROMPT,
            "schema": ChapterExtraction.model_json_schema(), "model": model_identity,
            "max_chars": max_chars, "chunker": "fixed200-v1",
        }, sort_keys=True, ensure_ascii=False))

    def ingest(
        self, project_id: str, chapter_no: int, title: str, content: str, *,
        retry=False, progress: Callable[[dict], None] | None = None, document=None,
    ) -> dict:
        paragraphs, blocks = split_chapter(content, self.max_chars)
        source = document.source(content, paragraphs) if document is not None else None
        job = self.repo.prepare(project_id, chapter_no, title, content, paragraphs, blocks, self.signature, source=source)
        settings = getattr(self.client, "settings", None)
        configuration = ({"provider": "deepseek", "base_url": settings.base_url, "model": settings.extraction_model,
                          "thinking": settings.extraction_thinking, "max_output_tokens": settings.max_output_tokens} if settings is not None else {"provider": "fake"})
        with self.repo.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO ingestion_specs VALUES(?,?,?,?)", (job["id"], self.model_identity, self.max_chars, json.dumps(configuration, sort_keys=True)))
        if job["status"] == "published":
            return {"job_id": job["id"], "status": "published", "cached": True, "model_calls": 0}
        owner = self.repo.claim(job["id"], retry=retry, lease_seconds=self.lease_seconds)
        calls = 0
        try:
            cached = self.repo.cached_blocks(job["id"])
            known = self.repo.known_entities(project_id)
            previous = ""
            results = []
            for block in blocks:
                self.repo.heartbeat(job["id"], owner, self.lease_seconds)
                if block.block_id in cached:
                    result = ChapterExtraction.model_validate(cached[block.block_id])
                    validate_evidence(result, block)
                else:
                    prompt = json.dumps({
                        "block_id": block.block_id, "known_entities": known,
                        "previous_summary": previous[-1500:],
                        "paragraphs": [{"paragraph_id": p.paragraph_id, "text": p.text} for p in block.paragraphs],
                    }, ensure_ascii=False)
                    try:
                        response = self.client.extract_json(prompt, ChapterExtraction, system_prompt=SYSTEM_PROMPT)
                    except LLMError as error:
                        if getattr(error, "call_id", None):
                            self._attempt(job["id"], block.block_id, error.call_id, getattr(error, "provider", "unknown"), 1, getattr(error, "usage", None), error.code)
                        raise
                    self._attempt(job["id"], block.block_id, response.call_id, response.provider, response.attempts, response.usage, "received")
                    calls += response.attempts
                    result = response.content
                    validate_evidence(result, block)
                    self.repo.save_block(job["id"], owner, block.block_id, result, response.call_id)
                previous = result.summary
                results.append((block, result))
                if progress:
                    progress({"job_id": job["id"], "completed_blocks": len(results), "total_blocks": len(blocks)})
            self._publish(project_id, job["id"], owner, results)
        except BaseException as error:
            code = error.code if isinstance(error, LLMError) else "interrupted" if isinstance(error, KeyboardInterrupt) else "validation_or_storage"
            self.repo.fail(job["id"], owner, code)
            raise
        return {"job_id": job["id"], "status": "published", "cached": False, "model_calls": calls}

    def _publish(self, project_id, job_id, owner, results):
        with self.repo.db.transaction() as conn:
            job = self.repo.require(conn, "ingestion_jobs", job_id)
            version = self.repo.require(conn, "chapter_versions", job["version_id"])
            chapter = self.repo.require(conn, "chapters", version["chapter_id"])
            if job["owner"] != owner or job["status"] != "running":
                raise StorageError("任务所有权已变化，未发布知识。")
            if chapter["latest_version_id"] != version["id"]:
                raise StorageError("章节在提取期间已有更新版本，未发布过期结果。")
            sources = {row["paragraph_id"]: row for row in conn.execute("SELECT * FROM source_chunks WHERE version_id=?", (version["id"],))}

            def evidence(kind, record_id, items, block):
                for item in items:
                    start, end = locate_evidence(item, block)
                    conn.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?)", (
                        uid(), kind, record_id, sources[item.paragraph_id]["id"], item.quote, start, end,
                    ))
                conn.execute("INSERT OR IGNORE INTO dependencies VALUES(?,?,?)", (job_id, kind, record_id))

            local_names = {}
            summaries = []
            for block, result in results:
                entity_ids = {}
                for draft in result.entities:
                    key = (draft.kind, draft.name)
                    candidates = set() if draft.distinct else self.repo._entity_candidates(
                        conn, project_id, draft.name, draft.kind, chapter["published_generation_id"]
                    )
                    if not draft.distinct and key in local_names:
                        candidates = local_names[key]
                    entity_id = next(iter(candidates)) if len(candidates) == 1 else uid()
                    if not conn.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone():
                        conn.execute("INSERT INTO entities(id,project_id,name,kind) VALUES(?,?,?,?)", (entity_id, project_id, draft.name, draft.kind))
                    entity_ids[draft.key] = entity_id
                    for label in [draft.name, *draft.aliases]:
                        local_names.setdefault((draft.kind, label), set()).add(entity_id)
                    for alias in [draft.name, *draft.aliases]:
                        conn.execute("INSERT INTO entity_aliases VALUES(?,?,?,?,0)", (uid(), entity_id, alias, job_id))
                    mention_id = uid()
                    conn.execute("INSERT INTO entity_mentions VALUES(?,?,?)", (mention_id, entity_id, job_id))
                    evidence("entity_mention", mention_id, draft.evidence, block)
                event_ids = {}
                for draft in result.events:
                    event_id = uid()
                    event_ids[draft.key] = event_id
                    conn.execute("INSERT INTO events(id,generation_id,title,summary,story_time) VALUES(?,?,?,?,?)", (event_id, job_id, draft.title, draft.summary, draft.story_time))
                    for participant in set(draft.participants):
                        conn.execute("INSERT OR IGNORE INTO event_entities VALUES(?,?)", (event_id, entity_ids[participant]))
                    evidence("event", event_id, draft.evidence, block)
                    for paragraph_id in dict.fromkeys(e.paragraph_id for e in draft.evidence):
                        conn.execute("INSERT INTO event_mentions VALUES(?,?,?,?)", (uid(), event_id, sources[paragraph_id]["id"], draft.mention_kind))
                for draft in result.facts:
                    fact_id = uid()
                    conn.execute("""INSERT INTO facts(id,generation_id,subject_id,predicate,object_value,
                        object_entity_id,knowledge_holder_id,effective_from_event_id,effective_until_event_id,certainty)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                        fact_id, job_id, entity_ids[draft.subject], draft.predicate, draft.object_value,
                        entity_ids.get(draft.object_entity), entity_ids.get(draft.knowledge_holder),
                        event_ids.get(draft.effective_from), event_ids.get(draft.effective_until), draft.certainty,
                    ))
                    evidence("fact", fact_id, draft.evidence, block)
                for draft in result.links:
                    source, target = event_ids[draft.source], event_ids[draft.target]
                    if draft.relation == "overlaps":
                        source, target = sorted((source, target))
                    old = conn.execute("SELECT id FROM event_links WHERE generation_id=? AND from_event_id=? AND to_event_id=? AND relation_type=?", (job_id, source, target, draft.relation)).fetchone()
                    link_id = old[0] if old else uid()
                    if not old:
                        conn.execute("INSERT INTO event_links(id,generation_id,from_event_id,to_event_id,relation_type) VALUES(?,?,?,?,?)", (link_id, job_id, source, target, draft.relation))
                    evidence("link", link_id, draft.evidence, block)
                for draft in result.foreshadowing:
                    thread_id = uid()
                    conn.execute("INSERT INTO threads(id,generation_id,title,description) VALUES(?,?,?,?)", (thread_id, job_id, draft.title, draft.description))
                    evidence("thread", thread_id, draft.evidence, block)
                summaries.append((block, result))
            summary_id = uid()
            summary = "\n".join(f"[{block.block_id}] {result.summary}" for block, result in summaries)
            conn.execute("INSERT INTO summaries(id,generation_id,text) VALUES(?,?,?)", (summary_id, job_id, summary))
            for block, result in summaries:
                evidence("summary", summary_id, result.summary_evidence, block)
            self.repo.rebuild_memory(conn, job_id)
            conn.execute("UPDATE ingestion_jobs SET status='published',owner=NULL,lease_until=NULL,error_code=NULL WHERE id=?", (job_id,))
            conn.execute("UPDATE chapters SET published_generation_id=? WHERE id=?", (job_id, chapter["id"]))
            from easy_writing.storage.revisions import align_generation
            align_generation(conn, project_id, chapter["published_generation_id"], job_id)
            self.repo.rebuild_memory(conn, job_id)

    def _attempt(self, job_id, block_id, call_id, provider, attempts, usage, status):
        with self.repo.db.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO ingestion_attempts VALUES(?,?,?,?,?,?,?)", (call_id, job_id, block_id, provider, attempts,
                json.dumps(usage.model_dump() if usage else {}, ensure_ascii=False), status))
