"""改稿依赖、重审与派生资料刷新。"""

import json

from easy_writing.services.review_scope import ReviewScope, page_bounds, text_value
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import RECORD_TABLES, now, uid
from easy_writing.storage.revisions import pending


class RevisionService:
    def __init__(self, repo):
        self.repo = repo

    def _record(self, conn, project, kind, record_id):
        if kind not in RECORD_TABLES:
            raise StorageError("依赖记录类型无效。")
        row = self.repo.require(conn, RECORD_TABLES[kind], record_id)
        if kind == "entity":
            if row["project_id"] != project:
                raise StorageError("实体不属于本项目。")
        elif row["generation_id"] not in {c["published_generation_id"] for c in self.repo.visible(conn, project)}:
            raise StorageError("记录不属于本项目当前版本。")
        return dict(row)

    def depend(self, project, source_kind, source_id, target_kind, target_id, *, reason):
        reason = text_value(reason, "依赖原因")
        if target_kind == "entity" or (source_kind, source_id) == (target_kind, target_id):
            raise StorageError("依赖目标须是其他知识记录，不支持以共享实体本身为待复核目标。")
        with self.repo.db.transaction() as conn:
            self._record(conn, project, source_kind, source_id)
            self._record(conn, project, target_kind, target_id)
            old = conn.execute("SELECT * FROM record_dependencies WHERE project_id=? AND source_type=? AND source_id=? AND target_type=? AND target_id=?", (project, source_kind, source_id, target_kind, target_id)).fetchone()
            if old:
                return dict(old)
            row = {"id": uid(), "project_id": project, "source_type": source_kind, "source_id": source_id,
                   "target_type": target_kind, "target_id": target_id, "reason": reason}
            conn.execute("INSERT INTO record_dependencies VALUES(:id,:project_id,:source_type,:source_id,:target_type,:target_id,:reason)", row)
            self.repo._audit(conn, project, "dependency", row["id"], {}, row, reason)
            return row

    def status(self, project, *, limit=50, offset=0, describe=False):
        page_bounds(limit, offset)
        with self.repo.db.connect() as conn:
            scope = ReviewScope(self.repo, conn, project)
            total = conn.execute("SELECT COUNT(*) FROM revision_reviews WHERE project_id=? AND status='pending'", (project,)).fetchone()[0]
            reviews = [dict(r) for r in conn.execute("SELECT * FROM revision_reviews WHERE project_id=? AND status='pending' ORDER BY rowid LIMIT ? OFFSET ?", (project, limit, offset))]
            stale_summaries = conn.execute("""SELECT COUNT(*) FROM summaries s JOIN chapters c ON c.published_generation_id=s.generation_id
                WHERE c.project_id=? AND s.is_stale=1""", (project,)).fetchone()[0]
            views = [{"name": r["name"], "stale": r["snapshot_id"] != scope.snap["snapshot_id"], "created_at": r["created_at"]} for r in conn.execute("SELECT * FROM derived_views WHERE project_id=?", (project,))]
            if describe:
                for review in reviews:
                    for side in ("source", "target"):
                        review[f"{side}_record"] = self._review_record(conn, project, review[f"{side}_type"], review[f"{side}_id"])
                plots = dict(conn.execute("SELECT id,name FROM plot_threads WHERE project_id=?", (project,)))
                for view in views:
                    if view["name"].startswith("plot:"):
                        view["plot_title"] = plots.get(view["name"].partition(":")[2])
            return {"project_id": project, "snapshot_id": scope.snap["snapshot_id"], "pending_total": total, "reviews": reviews,
                "next_offset": offset + limit if offset + limit < total else None, "stale_summary_count": stale_summaries,
                "derived_views": views, "coverage": scope.knowledge._coverage(conn, scope.snap),
                "dependency_count": conn.execute("SELECT COUNT(*) FROM record_dependencies WHERE project_id=?", (project,)).fetchone()[0],
                "automatic_semantic_dependency_detection": False}

    @staticmethod
    def _review_record(conn, project, kind, record_id):
        if kind == "entity":
            row = conn.execute("SELECT name FROM entities WHERE project_id=? AND id=?", (project, record_id)).fetchone()
            return {"title": row["name"]} if row else None
        table = RECORD_TABLES.get(kind)
        if not table:
            return None
        row = conn.execute(f"""SELECT r.*, c.chapter_no, v.revision,
            (c.published_generation_id=r.generation_id AND c.latest_version_id=v.id) AS is_current
            FROM {table} r JOIN ingestion_jobs j ON j.id=r.generation_id
            JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id
            WHERE c.project_id=? AND r.id=?""", (project, record_id)).fetchone()
        if not row:
            return None
        title = None
        if kind in ("event", "thread"):
            title = row["title"]
        elif kind == "fact":
            subject = conn.execute("SELECT name FROM entities WHERE id=?", (row["subject_id"],)).fetchone()
            title = " · ".join(filter(None, [subject["name"] if subject else "", row["predicate"], row["object_value"]]))
        elif kind == "link":
            endpoints = dict(conn.execute("SELECT id,title FROM events WHERE id IN (?,?)",
                                          (row["from_event_id"], row["to_event_id"])))
            title = " → ".join(endpoints.get(row[key], "未找到事件") for key in ("from_event_id", "to_event_id"))
        return {"title": title, "chapter_no": row["chapter_no"], "revision": row["revision"], "is_current": row["is_current"]}

    def resolve(self, project, review_id, *, reason):
        reason = text_value(reason, "复核原因")
        with self.repo.db.transaction() as conn:
            row = dict(self.repo.require(conn, "revision_reviews", review_id))
            if row["project_id"] != project:
                raise StorageError("复核项不属于本项目。")
            if row["status"] == "resolved":
                return row
            conn.execute("UPDATE revision_reviews SET status='resolved',resolution=? WHERE id=?", (reason, review_id))
            # 复核记录是作者决定，不触发相同依赖再次排队。
            conn.execute("INSERT INTO edit_history VALUES(?,?,?,?,?,?,?,?)", (uid(), project, "revision_review", review_id,
                json.dumps(row, ensure_ascii=False), json.dumps({"status": "resolved", "reason": reason}, ensure_ascii=False), reason, now()))
            return {**row, "status": "resolved", "resolution": reason}

    def refresh(self, project, *, provider=None):
        rebuilt, deferred = [], []
        with self.repo.db.transaction() as conn:
            self.repo.require(conn, "projects", project)
            for chapter in self.repo.visible(conn, project):
                generation = chapter["published_generation_id"]
                summary = conn.execute("SELECT * FROM summaries WHERE generation_id=? AND is_stale=1", (generation,)).fetchone()
                if not summary:
                    continue
                rows, has_pending = [], False
                for kind in ("event", "fact", "thread"):
                    for record in conn.execute(f"SELECT * FROM {RECORD_TABLES[kind]} WHERE generation_id=? AND review_status<>'rejected' ORDER BY id", (generation,)):
                        if pending(conn, kind, record["id"]):
                            has_pending = True
                            continue
                        label = "作者确认" if record["review_status"] == "confirmed" else "待确认"
                        if kind == "fact":
                            subject = self.repo.canonical(conn, record["subject_id"])
                            obj = self.repo.canonical(conn, record["object_entity_id"]) if record["object_entity_id"] else None
                            if subject["review_status"] == "rejected" or (obj and obj["review_status"] == "rejected"):
                                continue
                            viewpoint = ""
                            if record["knowledge_holder_id"]:
                                holder = self.repo.canonical(conn, record["knowledge_holder_id"])
                                viewpoint = f"角色「{holder['name']}」的认知或观点："
                            text = viewpoint + f"{subject['name']}｜{record['predicate']}｜{obj['name'] if obj else record['object_value']}"
                        else:
                            text = record["title"] + "：" + record["summary" if kind == "event" else "description"]
                            if kind == "thread":
                                text += "（作者状态 " + record["state"] + "）"
                        rows.append(f"[{label}][{kind}:{record['id']}] {text}")
                if has_pending:
                    deferred.append(chapter["chapter_no"])
                    continue
                text = "【结构化刷新摘要，含作者修订；列示顺序不是故事时间，原文引用仅用于溯源】\n" + "\n".join(rows)
                conn.execute("UPDATE summaries SET text=?,is_stale=0,review_status='unconfirmed' WHERE id=?", (text, summary["id"]))
                self.repo._audit(conn, project, "summary_refresh", summary["id"], dict(summary), {"text": text, "method": "current-record-digest-v1"}, "本地按当前知识重建摘要，不调用模型")
                self.repo.rebuild_memory(conn, generation)
                rebuilt.append(chapter["chapter_no"])
        graph = TimelineService(self.repo)
        timeline = graph.timeline(project)
        views = {"timeline": timeline}
        for plot in graph.plots(project):
            views["plot:" + plot["id"]] = graph.plot_view(plot["id"])
        from easy_writing.services.checking import CheckingService
        from easy_writing.retrieval.corpus import CorpusBuilder
        corpus = CorpusBuilder(self.repo).build(project)
        vectors = None
        if provider:
            from easy_writing.retrieval.index import VectorIndex
            vectors = VectorIndex(self.repo, provider).ensure(corpus)
            CorpusBuilder(self.repo).check(corpus)
        report = CheckingService(self.repo).run(project)
        with self.repo.db.transaction() as conn:
            scope = ReviewScope(self.repo, conn, project)
            if scope.snap["snapshot_id"] != corpus.snapshot_id or report["snapshot_id"] != corpus.snapshot_id:
                raise StorageError("刷新期间知识已变化，未保存混合快照的派生视图，请重新刷新。")
            for name, content in views.items():
                if content["snapshot_id"] != corpus.snapshot_id:
                    raise StorageError("视图与当前快照不一致，请重新刷新。")
                conn.execute("INSERT OR REPLACE INTO derived_views VALUES(?,?,?,?,?)", (project, name, corpus.snapshot_id, json.dumps(content, ensure_ascii=False), now()))
        return {"rebuilt_summary_chapters": rebuilt, "deferred_summary_chapters": deferred, "vectors": vectors,
            "views": list(views), "check_run_id": report["id"], "snapshot_id": corpus.snapshot_id, "real_model_calls": 0}
