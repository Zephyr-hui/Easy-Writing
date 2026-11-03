from easy_writing.retrieval.corpus import CorpusBuilder, MemoryRecord
from easy_writing.retrieval.hybrid import terms_for
from easy_writing.storage.database import StorageError


class SourceReader:
    def __init__(self, repo, corpus, *, max_reads=2, max_chars=1600):
        self.repo, self.corpus = repo, corpus
        self.max_reads, self.max_chars = max_reads, max_chars
        self.read_calls, self.read_fragments = 0, 0
        self.allowed = {record.key: record for record in corpus.records}

    def read(self, keys, *, version_id=None):
        if self.corpus.actor is not None:
            raise StorageError("角色认知范围内不扩读原文，避免旁白或同段内容泄露其他角色信息。")
        if self.read_calls >= self.max_reads:
            raise StorageError("已达到本次原文回查次数预算。")
        if not keys or len(keys) > 3:
            raise StorageError("每次回查需要 1～3 条已检索知识编号。")
        if any(key not in self.allowed for key in keys):
            raise StorageError("原文请求不在当前任务的可见知识范围。")
        CorpusBuilder(self.repo).check(self.corpus)
        self.read_calls += 1
        result, seen, remaining = [], set(), self.max_chars
        with self.repo.db.connect() as conn:
            for key in keys:
                for ref in self.allowed[key].evidence:
                    if version_id is not None and ref["version_id"] != version_id:
                        continue
                    source = conn.execute("""SELECT s.*,c.latest_version_id FROM source_chunks s
                        JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id WHERE s.id=?""", (ref["source_chunk_id"],)).fetchone()
                    if source is None or source["version_id"] != source["latest_version_id"]:
                        raise StorageError("原文版本已过期，请重新检索。")
                    if self.corpus.at_event is not None:
                        candidates = [dict(source) | {"text": ref["quote"], "quote_only": True}]
                    else:
                        candidates = [dict(row) for row in conn.execute("""SELECT * FROM source_chunks WHERE version_id=? AND scene_no=?
                            AND start_offset BETWEEN ? AND ? ORDER BY ABS(start_offset-?),start_offset LIMIT 3""", (
                            source["version_id"], source["scene_no"], source["start_offset"]-200,
                            source["end_offset"]+200, source["start_offset"]))]
                    for row in candidates:
                        if row["id"] in seen or remaining <= 0:
                            continue
                        text = row["text"][:remaining]
                        result.append({"citation": key, "version_id": row["version_id"], "paragraph_id": row["paragraph_id"],
                                       "scene_no": row["scene_no"], "text": text, "truncated": len(text) < len(row["text"]),
                                       "unit": "quote" if row.get("quote_only") else "local_paragraph",
                                       "unindexed": key.startswith("pending:")})
                        seen.add(row["id"])
                        remaining -= len(text)
        if version_id is not None and not result:
            raise StorageError("指定版本不属于当前可见证据。")
        self.read_fragments += len(result)
        return result

    def pending(self, query, *, chapter=None):
        if self.corpus.actor is not None or self.corpus.at_event is not None:
            return []
        if self.read_calls >= self.max_reads:
            return []
        CorpusBuilder(self.repo).check(self.corpus)
        terms = sorted(terms_for(query), key=len, reverse=True)[:12]
        if not terms and chapter is None:
            return []
        result = []
        with self.repo.db.connect() as conn:
            # 仅搜索已保存但尚未发布知识的最新版正文；不读任意工作区文件。
            clauses = " OR ".join("instr(s.text,?)>0" for _ in terms) or "1"
            for row in conn.execute("""SELECT s.*,c.chapter_no FROM source_chunks s JOIN chapters c ON c.latest_version_id=s.version_id
                LEFT JOIN ingestion_jobs j ON j.id=c.published_generation_id
                WHERE c.project_id=? AND (j.id IS NULL OR j.version_id<>c.latest_version_id)
                AND (? IS NULL OR c.chapter_no<=?) AND (? IS NULL OR c.chapter_no=?) AND (""" + clauses + ") ORDER BY c.chapter_no,s.start_offset LIMIT 2",
                (self.corpus.project_id, self.corpus.through_chapter, self.corpus.through_chapter, chapter, chapter, *terms)):
                result.append({"citation": "pending:" + row["id"], "version_id": row["version_id"],
                               "paragraph_id": row["paragraph_id"], "scene_no": row["scene_no"],
                               "text": row["text"][:min(self.max_chars, 400)], "unit": "pending_paragraph",
                               "unindexed": True, "chapter_no": row["chapter_no"]})
                key = "pending:" + row["id"]
                self.allowed[key] = MemoryRecord("pending", row["id"], "", row["text"], evidence=[{
                    "source_chunk_id": row["id"], "version_id": row["version_id"], "quote": row["text"]}])
                if len(result) >= 2:
                    break
        if result:
            self.read_calls += 1
            self.read_fragments += len(result)
        return result

    def paths(self, event_ids, *, max_roots=2, direction="in", relation="before", depth=2, max_nodes=8, details=False):
        if self.corpus.actor is not None:
            return []
        snap = self.corpus.snapshot
        result = []
        for event_id in list(dict.fromkeys(event_ids))[:max_roots]:
            if event_id not in snap["events"]:
                continue
            if self.corpus.at_event and CorpusBuilder(self.repo).knowledge._compare(snap, event_id, self.corpus.at_event) not in ("equal", "before"):
                raise StorageError("该事件不在当前允许的故事时点范围。")
            traversal = CorpusBuilder(self.repo).knowledge.timeline.neighbors(
                self.corpus.project_id, event_id, direction=direction, relation=relation, depth=depth,
                max_nodes=max_nodes, through_chapter=self.corpus.through_chapter)
            nodes = [snap["events"][event_id], *[item["event"] for item in traversal["events"]]]
            if self.corpus.at_event:
                nodes = [e for e in nodes if CorpusBuilder(self.repo).knowledge._compare(snap, e["id"], self.corpus.at_event) in ("equal", "before")]
            allowed = {e["id"] for e in nodes}
            for event in nodes:
                key = "event:" + event["id"]
                if key not in self.allowed:
                    with self.repo.db.connect() as conn:
                        refs = CorpusBuilder(self.repo).refs(conn, "event", event["source_event_ids"], snap)
                    self.allowed[key] = MemoryRecord("event", event["id"], event["generation_id"], event["title"] + "：" + event["summary"], refs, event_ids=[event["id"]])
            links = [{"id": l["id"], "source": l["source"], "target": l["target"], "type": l["relation_type"]} for l in traversal["links"] if l["source"] in allowed and l["target"] in allowed]
            if details:
                with self.repo.db.connect() as conn:
                    for link in links:
                        refs = CorpusBuilder(self.repo).refs(conn, "link", [link["id"]], snap)
                        link["key"] = "link:" + link["id"]
                        link["evidence"] = [{k: ref[k] for k in ("version_id", "chapter_no", "paragraph_id")} for ref in refs[:2]]
            result.append({"root": event_id, "nodes": [{"key": "event:"+e["id"], "title": e["title"]} for e in nodes],
                           "links": links,
                           "truncated": traversal["truncated"]})
        CorpusBuilder(self.repo).check(self.corpus)
        return result
