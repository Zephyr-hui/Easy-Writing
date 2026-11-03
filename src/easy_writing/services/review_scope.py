"""检查服务共用的当前版本快照；不从缺少记录推断事实为假。"""

from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import event_root
from easy_writing.storage.database import StorageError


def page_bounds(limit, offset):
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 500 or offset < 0:
        raise StorageError("分页参数无效：limit 应为 1～500，offset 应为非负整数。")


def text_value(value, label, max_length=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise StorageError(f"{label}不能为空且长度不能超过 {max_length}。")
    return value.strip()


class ReviewScope:
    def __init__(self, repo, conn, project_id, through_chapter=None):
        self.repo, self.conn = repo, conn
        self.knowledge = KnowledgeService(repo)
        self.graph = self.knowledge.timeline
        self.snap = self.graph._snapshot(conn, project_id, through_chapter)
        self.project_id = project_id
        self.catalog = self.knowledge._catalog(conn, self.snap)
        self.versions = {c["knowledge_version_id"] for c in self.snap["generations"].values()}

    def compare(self, left, right):
        return self.knowledge._compare(self.snap, left, right)

    def event(self, value, *, allow_review=False):
        root = event_root(self.conn, value, self.snap["through_chapter"])
        if allow_review:
            row = self.conn.execute("SELECT generation_id,review_status FROM events WHERE id=?", (root,)).fetchone()
            if row and row["generation_id"] in self.snap["generations"] and row["review_status"] != "rejected":
                return root  # 仅供作者查看 / 重审当前标注，不进入模型知识快照。
        if root not in self.snap["events"] or (value not in self.snap["raw_to_root"] and value != root):
            raise StorageError("事件不属于本项目当前可见版本。")
        return root

    def entity(self, value):
        return self.knowledge._resolve(self.conn, self.snap, value)

    def current(self, table, record_id):
        if table not in ("facts", "threads"):
            raise StorageError("无效记录类型。")
        row = self.repo.require(self.conn, table, record_id)
        if row["generation_id"] not in self.snap["generations"] or row["review_status"] == "rejected":
            raise StorageError("记录不属于本项目当前可见版本，或已被否决。")
        return dict(row)

    def refs(self, kind, record_ids):
        result = []
        for record_id in record_ids:
            for r in self.conn.execute("""SELECT e.id AS evidence_id,e.record_type,e.record_id,
                e.source_chunk_id,e.quote,e.quote_start,e.quote_end,s.text,s.version_id,
                s.paragraph_id,s.start_offset,c.chapter_no FROM evidence_links e
                JOIN source_chunks s ON s.id=e.source_chunk_id
                JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id
                WHERE e.record_type=? AND e.record_id=? ORDER BY c.chapter_no,s.start_offset,e.id""", (kind, record_id)):
                if r["version_id"] not in self.versions:
                    continue
                if r["text"][r["quote_start"]:r["quote_end"]] != r["quote"]:
                    continue
                item = dict(r)
                item.pop("text")
                item["absolute_start"] = r["start_offset"] + r["quote_start"]
                result.append(item)
        return result

    def event_refs(self, event_id):
        return self.refs("event", self.snap["events"][event_id]["source_event_ids"])

    def evidence(self, evidence_id):
        row = self.repo.require(self.conn, "evidence_links", evidence_id)
        refs = self.refs(row["record_type"], [row["record_id"]])
        match = next((r for r in refs if r["evidence_id"] == evidence_id), None)
        if match is None:
            raise StorageError("证据不是本项目当前可见版本的有效原文。")
        return match

    def facts(self):
        result = []
        for generation in self.snap["generations"]:
            if generation in self.snap.get("quarantined_generations", ()):
                continue
            for row in self.conn.execute("SELECT * FROM facts WHERE generation_id=? AND review_status<>'rejected' ORDER BY id", (generation,)):
                from easy_writing.storage.revisions import pending
                if pending(self.conn, "fact", row["id"]):
                    continue
                sid = self.repo.canonical(self.conn, row["subject_id"])["id"]
                oid = self.repo.canonical(self.conn, row["object_entity_id"])["id"] if row["object_entity_id"] else None
                if sid not in self.catalog or (oid and oid not in self.catalog):
                    continue
                refs = self.refs("fact", [row["id"]])
                if not refs:
                    continue
                anchors = []
                for value in (row["effective_from_event_id"], row["effective_until_event_id"]):
                    root = event_root(self.conn, value, self.snap["through_chapter"]) if value else None
                    anchors.append(root if root in self.snap["events"] else None)
                mode = self.conn.execute("SELECT mode FROM fact_modes WHERE fact_id=?", (row["id"],)).fetchone()
                holder = self.repo.canonical(self.conn, row["knowledge_holder_id"])["id"] if row["knowledge_holder_id"] else None
                result.append({**dict(row), "subject_id": sid, "object_entity_id": oid,
                    "subject_name": self.catalog[sid]["name"], "subject_kind": self.catalog[sid]["kind"],
                    "object_name": self.catalog[oid]["name"] if oid else row["object_value"],
                    "value_key": f"entity:{oid}" if oid else f"text:{row['object_value']}",
                    "knowledge_holder_id": holder, "from_event": anchors[0], "until_event": anchors[1],
                    "hidden_anchor": any(v is not None and a is None for v, a in zip((row["effective_from_event_id"], row["effective_until_event_id"]), anchors)),
                    "mode": mode[0] if mode else "assertion", "evidence": refs})
        return result

    def touch(self, kind, record_id, before, after, reason):
        self.repo._audit(self.conn, self.project_id, kind, record_id, before, after, reason)
        self.graph._touch(self.conn, self.project_id)
