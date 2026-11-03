import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from easy_writing.storage.database import Database, StorageError


def uid():
    return uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


RECORD_TABLES = {
    "entity": "entities", "event": "events", "fact": "facts",
    "thread": "threads", "summary": "summaries", "link": "event_links",
}


class Repository:
    def __init__(self, database: Database):
        self.db = database

    @staticmethod
    def require(conn, table: str, record_id: str):
        # table 只能由程序常量提供，用户值始终使用绑定参数。
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise StorageError("指定记录不存在。")
        return row

    def create_project(self, name: str) -> dict:
        name = name.strip()
        if not name or len(name) > 200:
            raise StorageError("项目名称应为 1～200 个字符。")
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO projects VALUES(?,?,?)", (uid(), name, now()))
            return dict(conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone())

    def projects(self) -> list[dict]:
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY created_at,id")]

    def prepare(self, project_id, chapter_no, title, content, paragraphs, blocks, signature, *, source=None):
        if chapter_no <= 0:
            raise StorageError("章节编号必须大于零。")
        if not title.strip() or len(title) > 300:
            raise StorageError("章节标题应为 1～300 个字符。")
        with self.db.transaction() as conn:
            self.require(conn, "projects", project_id)
            chapter = conn.execute(
                "SELECT * FROM chapters WHERE project_id=? AND chapter_no=?", (project_id, chapter_no)
            ).fetchone()
            if chapter is None:
                chapter_id = uid()
                conn.execute(
                    "INSERT INTO chapters(id,project_id,chapter_no,title) VALUES(?,?,?,?)",
                    (chapter_id, project_id, chapter_no, title),
                )
            else:
                chapter_id = chapter["id"]
            version = conn.execute(
                "SELECT * FROM chapter_versions WHERE chapter_id=? AND content_hash=?",
                (chapter_id, digest(content)),
            ).fetchone()
            if version is None:
                version_id = uid()
                revision = conn.execute(
                    "SELECT COALESCE(MAX(revision),0)+1 FROM chapter_versions WHERE chapter_id=?", (chapter_id,)
                ).fetchone()[0]
                conn.execute("INSERT INTO chapter_versions VALUES(?,?,?,?,?,?)", (
                    version_id, chapter_id, revision, digest(content), content, now(),
                ))
                for p in paragraphs:
                    conn.execute("INSERT INTO source_chunks VALUES(?,?,?,?,?,?,?)", (
                        uid(), version_id, p.paragraph_id, p.scene_no, p.start, p.end, p.text,
                    ))
            else:
                version_id = version["id"]
            if source is not None:
                conn.execute("INSERT OR IGNORE INTO chapter_file_sources VALUES(?,?,?,?,?,?)", (
                    version_id, source["file_hash"], source["filename"], source["format"],
                    json.dumps(source["locations"], ensure_ascii=False, separators=(",", ":")), now(),
                ))
            conn.execute("UPDATE chapters SET latest_version_id=?,title=? WHERE id=?", (version_id, title, chapter_id))
            if chapter is not None and chapter["latest_version_id"] != version_id:
                from easy_writing.storage.revisions import chapter_revised
                chapter_revised(conn, project_id, chapter_id, chapter["latest_version_id"], version_id, chapter["published_generation_id"])
            job = conn.execute("SELECT * FROM ingestion_jobs WHERE version_id=? AND signature=?", (version_id, signature)).fetchone()
            if job is None:
                job_id = uid()
                conn.execute(
                    "INSERT INTO ingestion_jobs(id,version_id,signature,status,total_blocks,created_at) VALUES(?,?,?,'pending',?,?)",
                    (job_id, version_id, signature, len(blocks), now()),
                )
            else:
                job_id = job["id"]
                if job["status"] == "published":
                    conn.execute("UPDATE chapters SET published_generation_id=? WHERE id=?", (job_id, chapter_id))
            return dict(self.require(conn, "ingestion_jobs", job_id))

    def claim(self, job_id: str, *, retry: bool, lease_seconds: float) -> str:
        owner = uid()
        with self.db.transaction() as conn:
            job = self.require(conn, "ingestion_jobs", job_id)
            if job["status"] == "running" and (job["lease_until"] or 0) > time.time():
                raise StorageError("同一入库任务正在运行，请勿重复提交。")
            if job["status"] in ("failed", "running") and not retry:
                raise StorageError("存在失败或中断的任务，使用 --retry 复用已完成分块继续。")
            if job["status"] == "published":
                raise StorageError("任务已经发布。")
            conn.execute("UPDATE ingestion_jobs SET status='running',owner=?,lease_until=?,error_code=NULL WHERE id=?", (
                owner, time.time() + lease_seconds, job_id,
            ))
        return owner

    def heartbeat(self, job_id, owner, lease_seconds):
        with self.db.transaction() as conn:
            result = conn.execute(
                "UPDATE ingestion_jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'",
                (time.time() + lease_seconds, job_id, owner),
            )
            if result.rowcount != 1:
                raise StorageError("入库任务所有权已变化，请检查任务状态。")

    def cached_blocks(self, job_id) -> dict:
        with self.db.connect() as conn:
            return {row["block_id"]: json.loads(row["result_json"]) for row in conn.execute(
                "SELECT * FROM extraction_blocks WHERE job_id=?", (job_id,)
            )}

    def save_block(self, job_id, owner, block_id, result, call_id):
        with self.db.transaction() as conn:
            job = self.require(conn, "ingestion_jobs", job_id)
            if job["owner"] != owner or job["status"] != "running":
                raise StorageError("入库任务所有权已变化。")
            conn.execute("INSERT OR REPLACE INTO extraction_blocks VALUES(?,?,?,?)", (job_id, block_id, result.model_dump_json(), call_id))
            conn.execute("UPDATE ingestion_jobs SET completed_blocks=(SELECT COUNT(*) FROM extraction_blocks WHERE job_id=?) WHERE id=?", (job_id, job_id))

    def fail(self, job_id, owner, code):
        with self.db.transaction() as conn:
            conn.execute("UPDATE ingestion_jobs SET status='failed',error_code=?,owner=NULL,lease_until=NULL WHERE id=? AND owner=?", (code, job_id, owner))

    def job(self, job_id):
        with self.db.connect() as conn:
            return dict(self.require(conn, "ingestion_jobs", job_id))

    @staticmethod
    def canonical(conn, entity_id):
        seen = set()
        while entity_id:
            if entity_id in seen:
                raise StorageError("人物合并关系存在环，请检查数据库。")
            seen.add(entity_id)
            entity = Repository.require(conn, "entities", entity_id)
            if not entity["merged_into"]:
                return entity
            entity_id = entity["merged_into"]

    @staticmethod
    def visible(conn, project_id, include_stale=False):
        sql = """SELECT c.*, j.version_id AS knowledge_version_id,
                 v.revision AS knowledge_revision, lv.revision AS latest_revision
                 FROM chapters c LEFT JOIN ingestion_jobs j ON j.id=c.published_generation_id
                 LEFT JOIN chapter_versions v ON v.id=j.version_id
                 JOIN chapter_versions lv ON lv.id=c.latest_version_id WHERE c.project_id=?"""
        rows = conn.execute(sql + " ORDER BY c.chapter_no", (project_id,)).fetchall()
        return [row for row in rows if row["published_generation_id"] and (include_stale or row["latest_version_id"] == row["knowledge_version_id"])]

    def chapters(self, project_id):
        with self.db.connect() as conn:
            self.require(conn, "projects", project_id)
            rows = conn.execute("""SELECT c.id,c.chapter_no,c.title,lv.revision AS latest_revision,
                j.version_id AS published_version_id,c.latest_version_id,c.published_generation_id
                FROM chapters c JOIN chapter_versions lv ON lv.id=c.latest_version_id
                LEFT JOIN ingestion_jobs j ON j.id=c.published_generation_id
                WHERE c.project_id=? ORDER BY c.chapter_no""", (project_id,)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                latest_job = conn.execute("SELECT id,status,total_blocks,completed_blocks,error_code FROM ingestion_jobs WHERE version_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (row["latest_version_id"],)).fetchone()
                item["latest_job"] = dict(latest_job) if latest_job else None
                item["knowledge_is_current"] = row["published_version_id"] == row["latest_version_id"]
                result.append(item)
            return result

    @staticmethod
    def _entity_candidates(conn, project_id, name, kind, previous_generation=None):
        rows = conn.execute("""SELECT DISTINCT e.id FROM entities e
            LEFT JOIN entity_aliases a ON a.entity_id=e.id
            WHERE e.project_id=? AND e.kind=? AND e.review_status<>'rejected'
            AND (e.name=? OR (a.alias=? AND (a.author_confirmed=1 OR a.generation_id=? OR EXISTS(
              SELECT 1 FROM chapters ac JOIN ingestion_jobs aj ON aj.id=ac.published_generation_id
              WHERE ac.published_generation_id=a.generation_id AND ac.latest_version_id=aj.version_id
            )))) AND (
              EXISTS(SELECT 1 FROM entity_mentions m JOIN chapters c ON c.published_generation_id=m.generation_id
                JOIN ingestion_jobs j ON j.id=m.generation_id
                WHERE m.entity_id=e.id AND (c.latest_version_id=j.version_id OR m.generation_id=?))
              OR e.review_status='confirmed')""", (project_id, kind, name, name, previous_generation, previous_generation)).fetchall()
        roots = [Repository.canonical(conn, row["id"]) for row in rows]
        return {row["id"] for row in roots if row["review_status"] != "rejected"}

    def known_entities(self, project_id, limit=30):
        entities = self.records(project_id, "entity", limit=limit)
        return [{"id": row["id"], "name": row["name"], "kind": row["kind"], "aliases": row["aliases"]} for row in entities]

    def records(self, project_id, kind, *, include_stale=False, limit=100, offset=0):
        if kind not in RECORD_TABLES or not 1 <= limit <= 500 or offset < 0:
            raise StorageError("查询类型或分页参数无效。")
        with self.db.connect() as conn:
            self.require(conn, "projects", project_id)
            chapters = self.visible(conn, project_id, include_stale)
            from easy_writing.storage.revisions import pending_generations
            quarantined = pending_generations(conn, project_id)
            result = []
            if kind == "entity":
                seen = {}
                for chapter in chapters:
                    for row in conn.execute("SELECT e.* FROM entities e JOIN entity_mentions m ON m.entity_id=e.id WHERE m.generation_id=? ORDER BY e.name,e.id", (chapter["published_generation_id"],)):
                        root = self.canonical(conn, row["id"])
                        if root["review_status"] == "rejected":
                            continue
                        item = seen.setdefault(root["id"], {**dict(root), "aliases": [], "mention_ids": [], "has_stale_mentions": False})
                        item["has_stale_mentions"] |= chapter["latest_version_id"] != chapter["knowledge_version_id"]
                        item["aliases"].extend(a[0] for a in conn.execute("SELECT alias FROM entity_aliases WHERE entity_id=? AND author_confirmed=1", (root["id"],)))
                        item["aliases"].extend([row["name"]] + [a[0] for a in conn.execute(
                            "SELECT alias FROM entity_aliases WHERE entity_id=? AND (author_confirmed=1 OR generation_id=?)", (row["id"], chapter["published_generation_id"])
                        )])
                        item["mention_ids"].extend([m[0] for m in conn.execute("SELECT id FROM entity_mentions WHERE entity_id=? AND generation_id=?", (row["id"], chapter["published_generation_id"]))])
                for item in seen.values():
                    item["aliases"] = sorted(set(item["aliases"]) - {item["name"]})
                    result.append(item)
            else:
                table = RECORD_TABLES[kind]
                for chapter in chapters:
                    for row in conn.execute(f"SELECT * FROM {table} WHERE generation_id=? AND review_status<>'rejected' ORDER BY rowid", (chapter["published_generation_id"],)):
                        item = dict(row)
                        from easy_writing.storage.revisions import pending
                        item["requires_review"] = row["generation_id"] in quarantined or pending(conn, kind, row["id"])
                        item.update(chapter_no=chapter["chapter_no"], knowledge_is_current=chapter["latest_version_id"] == chapter["knowledge_version_id"])
                        if kind == "fact":
                            subject = self.canonical(conn, row["subject_id"])
                            if subject["review_status"] == "rejected":
                                continue
                            item["subject_name"] = subject["name"]
                            if row["object_entity_id"]:
                                obj = self.canonical(conn, row["object_entity_id"])
                                if obj["review_status"] == "rejected":
                                    continue
                                item["object_entity_name"] = obj["name"]
                        if kind == "summary" and row["is_stale"] and not include_stale:
                            continue
                        item["author_review_count"] = conn.execute("SELECT COUNT(*) FROM edit_history WHERE record_type=? AND record_id=?", (kind, row["id"])).fetchone()[0]
                        result.append(item)
            return result[offset:offset + limit]

    def evidence(self, kind, record_id):
        with self.db.connect() as conn:
            if kind == "entity":
                self.require(conn, "entities", record_id)
                # 保留原始身份和合并身份的全部出处，回查包含历史版本并显式标记。
                sources = [r[0] for r in conn.execute("SELECT id FROM entities") if self.canonical(conn, r[0])["id"] == self.canonical(conn, record_id)["id"]]
                record_ids = [r[0] for source in sources for r in conn.execute("SELECT id FROM entity_mentions WHERE entity_id=?", (source,))]
                evidence_kind = "entity_mention"
            elif kind == "event":
                from easy_writing.services.timeline import TimelineService, event_root
                project = TimelineService(self)._project_for_event(conn, record_id)
                target = event_root(conn, record_id)
                rows = conn.execute("""SELECT e.id FROM events e JOIN ingestion_jobs j ON j.id=e.generation_id
                    JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id WHERE c.project_id=?""", (project,)).fetchall()
                record_ids = [r[0] for r in rows if event_root(conn, r[0]) == target]
                evidence_kind = "event"
            elif kind in RECORD_TABLES:
                self.require(conn, RECORD_TABLES[kind], record_id)
                record_ids, evidence_kind = [record_id], kind
            else:
                raise StorageError("未知的证据记录类型。")
            result = []
            files = {}
            for rid in record_ids:
                for row in conn.execute("""SELECT e.*,s.text AS paragraph_text,s.paragraph_id,s.start_offset,
                    v.id AS version_id,v.revision,c.chapter_no,c.title,c.latest_version_id
                    FROM evidence_links e JOIN source_chunks s ON s.id=e.source_chunk_id
                    JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id
                    WHERE e.record_type=? AND e.record_id=? ORDER BY c.chapter_no,s.start_offset""", (evidence_kind, rid)):
                    item = dict(row)
                    item["absolute_start"] = row["start_offset"] + row["quote_start"]
                    item["absolute_end"] = row["start_offset"] + row["quote_end"]
                    item["is_latest_source"] = row["version_id"] == row["latest_version_id"]
                    if row["version_id"] not in files:
                        files[row["version_id"]] = [
                            ({"filename": f["filename"], "format": f["format"], "sha256": f["file_hash"]}, json.loads(f["locations_json"]))
                            for f in conn.execute("""SELECT * FROM chapter_file_sources WHERE version_id=?
                                ORDER BY created_at,file_hash,filename""", (row["version_id"],))
                        ]
                    item["document_sources"] = [origin | mapping.get(row["paragraph_id"], {})
                                                for origin, mapping in files[row["version_id"]]]
                    result.append(item)
            return result

    def history(self, project_id, *, record_id=None, limit=100, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise StorageError("分页参数无效。")
        with self.db.connect() as conn:
            self.require(conn, "projects", project_id)
            sql = "SELECT * FROM edit_history WHERE project_id=?"
            params = [project_id]
            if record_id:
                sql += " AND record_id=?"
                params.append(record_id)
            rows = conn.execute(sql + " ORDER BY created_at,id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            return [{**dict(row), "before_json": json.loads(row["before_json"]), "after_json": json.loads(row["after_json"])} for row in rows]

    @staticmethod
    def rebuild_memory(conn, generation_id):
        conn.execute("DELETE FROM memory_items WHERE generation_id=?", (generation_id,))
        for kind, table in RECORD_TABLES.items():
            if kind in ("entity", "link"):
                continue
            for row in conn.execute(f"SELECT * FROM {table} WHERE generation_id=? AND review_status<>'rejected'", (generation_id,)).fetchall():
                if kind == "summary":
                    if row["is_stale"]:
                        continue
                    text = row["text"]
                elif kind == "fact":
                    subject = Repository.canonical(conn, row["subject_id"])
                    obj_entity = Repository.canonical(conn, row["object_entity_id"]) if row["object_entity_id"] else None
                    if subject["review_status"] == "rejected" or (obj_entity and obj_entity["review_status"] == "rejected"):
                        continue
                    obj = obj_entity["name"] if obj_entity else row["object_value"]
                    text = f"{subject['name']}｜{row['predicate']}｜{obj}"
                else:
                    text = row["title"] + "：" + row["summary" if kind == "event" else "description"]
                    if kind == "thread":
                        text += "｜状态：" + row["state"]
                conn.execute("INSERT INTO memory_items VALUES(?,?,?,?,?)", (uid(), generation_id, kind, row["id"], text))

    def impact(self, kind, record_id):
        if kind not in RECORD_TABLES:
            raise StorageError("记录类型无效。")
        with self.db.connect() as conn:
            row = self.require(conn, RECORD_TABLES[kind], record_id)
            if kind == "entity":
                project_id = row["project_id"]
                # 人物合并或改名可能影响多章；先保守失效本项目已发布摘要。
                ids = [r[0] for r in conn.execute("SELECT published_generation_id FROM chapters WHERE project_id=? AND published_generation_id IS NOT NULL", (project_id,))]
            else:
                ids = [row["generation_id"]]
                project_id = conn.execute("SELECT c.project_id FROM chapters c JOIN chapter_versions v ON v.chapter_id=c.id JOIN ingestion_jobs j ON j.version_id=v.id WHERE j.id=?", (ids[0],)).fetchone()[0]
            return {"project_id": project_id, "generation_ids": ids, "summaries_will_be_stale": kind != "summary"}

    @staticmethod
    def _audit(conn, project_id, kind, record_id, before, after, reason):
        conn.execute("INSERT INTO edit_history VALUES(?,?,?,?,?,?,?,?)", (
            uid(), project_id, kind, record_id, json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False), reason, now(),
        ))
        from easy_writing.storage.revisions import changed
        source_kind = {"event_time": "event", "event_identity": "event", "fact_context": "fact", "fact_awareness": "fact", "thread_state": "thread"}.get(kind, kind)
        changed(conn, project_id, kind, record_id, {"reason": reason, "changed_fields": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}, origins=[(source_kind, record_id)])

    @staticmethod
    def _refresh(conn, impact, kind, *, invalidate_summary=True):
        for generation_id in impact["generation_ids"]:
            if kind != "summary" and invalidate_summary:
                conn.execute("UPDATE summaries SET is_stale=1 WHERE generation_id=?", (generation_id,))
            Repository.rebuild_memory(conn, generation_id)

    def edit(self, kind, record_id, *, reason, field=None, value=None, status=None):
        allowed = {"entity": {"name"}, "event": {"title", "summary", "story_time"}, "fact": {"predicate", "object_value"}, "thread": {"title", "description"}, "summary": {"text"}, "link": set()}
        if kind not in allowed or not reason.strip() or (field is None and status is None):
            raise StorageError("请提供有效记录类型、修改字段或状态，以及修改原因。")
        if field is not None and (field not in allowed[kind] or value is None or not value.strip() or len(value) > 2000):
            raise StorageError("修改字段或内容无效。")
        if status is not None and status not in ("confirmed", "rejected", "unconfirmed"):
            raise StorageError("确认状态无效。")
        if kind == "link":
            from easy_writing.services.timeline import TimelineService
            return TimelineService(self).review_link(record_id, status, reason)
        impact = self.impact(kind, record_id)
        with self.db.transaction() as conn:
            before = dict(self.require(conn, RECORD_TABLES[kind], record_id))
            if kind == "entity" and before["merged_into"]:
                raise StorageError("该实体已经合并，请修改目标实体。")
            if kind == "fact" and field == "object_value":
                # 作者修正为文字对象后，旧的实体指向不能继续覆盖显示值。
                conn.execute("UPDATE facts SET object_entity_id=NULL WHERE id=?", (record_id,))
            if field:
                conn.execute(f"UPDATE {RECORD_TABLES[kind]} SET {field}=? WHERE id=?", (value.strip(), record_id))
                status = status or "confirmed"
                if kind == "summary":
                    conn.execute("UPDATE summaries SET is_stale=0 WHERE id=?", (record_id,))
            if status:
                conn.execute(f"UPDATE {RECORD_TABLES[kind]} SET review_status=? WHERE id=?", (status, record_id))
            if kind == "entity" and field == "name":
                conn.execute("INSERT INTO entity_aliases VALUES(?,?,?,?,1)", (uid(), record_id, before["name"], None))
            after = dict(self.require(conn, RECORD_TABLES[kind], record_id))
            self._audit(conn, impact["project_id"], kind, record_id, before, after, reason)
            self._refresh(conn, impact, kind, invalidate_summary=bool(field) or status == "rejected" or before["review_status"] == "rejected")
        return {"record": after, "impact": impact}

    def add_alias(self, entity_id, alias, reason):
        if not alias.strip() or len(alias) > 200 or not reason.strip():
            raise StorageError("别名和修改原因不能为空，别名最多 200 字符。")
        with self.db.transaction() as conn:
            root = self.canonical(conn, entity_id)
            conn.execute("INSERT INTO entity_aliases VALUES(?,?,?,?,1)", (uid(), root["id"], alias.strip(), None))
            self._audit(conn, root["project_id"], "entity", root["id"], {}, {"author_alias": alias.strip()}, reason)
        return {"entity_id": root["id"], "alias": alias.strip()}

    def merge(self, source_id, target_id, reason):
        if not reason.strip():
            raise StorageError("合并需要填写原因。")
        impact = self.impact("entity", source_id)
        with self.db.transaction() as conn:
            source = self.canonical(conn, source_id)
            target = self.canonical(conn, target_id)
            if source["id"] == target["id"]:
                raise StorageError("两个实体已经是同一身份，不能自合并。")
            if source["project_id"] != target["project_id"] or source["kind"] != target["kind"]:
                raise StorageError("只能合并同一项目、同一类型的实体。")
            conn.execute("UPDATE entities SET merged_into=? WHERE id=?", (target["id"], source["id"]))
            conn.execute("UPDATE entities SET review_status='confirmed' WHERE id=?", (target["id"],))
            conn.execute("INSERT INTO entity_aliases VALUES(?,?,?,?,1)", (uid(), target["id"], source["name"], None))
            self._audit(conn, source["project_id"], "entity", source["id"], dict(source), {"merged_into": target["id"]}, reason)
            self._refresh(conn, impact, "entity")
        return {"source_id": source["id"], "target_id": target["id"], "impact": impact}
