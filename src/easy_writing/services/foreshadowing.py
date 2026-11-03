"""伏笔生命周期仅由作者确认推进；自动关联永远只是建议。"""

from collections import Counter

from easy_writing.services.review_scope import ReviewScope, page_bounds, text_value
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import now, uid

STATES = ("candidate", "planted", "reinforced", "partially_resolved", "resolved", "cancelled", "open")
TRANSITIONS = {
    "candidate": {"planted", "cancelled", "open"},
    "planted": {"reinforced", "partially_resolved", "resolved", "cancelled", "open"},
    "reinforced": {"reinforced", "partially_resolved", "resolved", "cancelled", "open"},
    "partially_resolved": {"reinforced", "partially_resolved", "resolved", "cancelled", "open"},
    "resolved": set(), "cancelled": set(), "open": {"planted", "reinforced", "partially_resolved", "resolved", "cancelled"},
}


class ForeshadowingService:
    def __init__(self, repo):
        self.repo = repo

    @staticmethod
    def _rows(scope):
        rows = []
        for generation, chapter in scope.snap["generations"].items():
            for row in scope.conn.execute("SELECT * FROM threads WHERE generation_id=? AND review_status<>'rejected' ORDER BY id", (generation,)):
                transitions, skipped = [], []
                state = "candidate"
                for transition in scope.conn.execute("SELECT * FROM thread_transitions WHERE thread_id=? ORDER BY seq", (row["id"],)):
                    item = dict(transition)
                    try:
                        if item["event_id"]:
                            item["event_id"] = scope.event(item["event_id"], allow_review=True)
                        if item["evidence_id"]:
                            scope.evidence(item["evidence_id"])
                        if scope.snap["through_chapter"] is not None and not item["event_id"] and not item["evidence_id"]:
                            continue  # 仅作者可见的决定不进入章节视图。
                    except StorageError:
                        if scope.snap["through_chapter"] is None:
                            skipped.append(item["id"])
                        continue
                    if item["correction"]:
                        skipped.clear()
                    state = item["to_state"]
                    transitions.append(item)
                from easy_writing.storage.revisions import pending
                rows.append({**dict(row), "state": state, "stored_state": row["state"] if scope.snap["through_chapter"] is None else None,
                    "chapter_no": chapter["chapter_no"], "evidence": scope.refs("thread", [row["id"]]),
                    "transitions": transitions, "state_needs_review": bool(skipped) or pending(scope.conn, "thread", row["id"]),
                    "skipped_transition_count": len(skipped)})
        return sorted(rows, key=lambda r: (r["chapter_no"], r["id"]))

    def list(self, project_id, *, state=None, through_chapter=None, limit=100, offset=0):
        page_bounds(limit, offset)
        if state is not None and state not in STATES:
            raise StorageError("伏笔状态无效。")
        with self.repo.db.connect() as conn:
            scope = ReviewScope(self.repo, conn, project_id, through_chapter)
            rows = self._rows(scope)
            counts = dict(Counter(r["state"] for r in rows))
            selected = [r for r in rows if state is None or r["state"] == state]
            return {"threads": selected[offset:offset + limit], "total": len(selected), "state_counts": counts,
                "next_offset": offset + limit if offset + limit < len(selected) else None,
                "snapshot_id": scope.snap["snapshot_id"], "coverage": scope.knowledge._coverage(conn, scope.snap),
                "unresolved_is_error": False}

    def create(self, project_id, title, description, *, event_id, reason):
        title = text_value(title, "伏笔标题")
        description = text_value(description, "伏笔描述")
        reason = text_value(reason, "作者原因")
        with self.repo.db.transaction() as conn:
            scope = ReviewScope(self.repo, conn, project_id)
            event = scope.event(event_id)
            refs = scope.event_refs(event)
            if not refs:
                raise StorageError("伏笔锚点需要有效事件原文证据。")
            generation = scope.snap["events"][event]["generation_id"]
            # 身份合并后的 canonical event 可能来自旧代，取当前提及所属代。
            if generation not in scope.snap["generations"]:
                generation = self.repo.require(conn, "events", scope.snap["events"][event]["source_event_ids"][0])["generation_id"]
            record_id = uid()
            conn.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (record_id, generation, title, description, "candidate", "confirmed"))
            for ref in refs:
                conn.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?)", (uid(), "thread", record_id, ref["source_chunk_id"], ref["quote"], ref["quote_start"], ref["quote_end"]))
            after = dict(self.repo.require(conn, "threads", record_id))
            scope.touch("thread", record_id, {}, {**after, "anchor_event": event}, reason)
            self.repo._refresh(conn, {"generation_ids": [generation]}, "thread")
            return after

    def transition(self, project_id, thread_id, state, *, reason, event_id=None, evidence_id=None, correction=False):
        if state not in STATES:
            raise StorageError("伏笔状态无效。")
        reason = text_value(reason, "作者原因")
        with self.repo.db.transaction() as conn:
            scope = ReviewScope(self.repo, conn, project_id)
            before = scope.current("threads", thread_id)
            effective = next(r for r in self._rows(scope) if r["id"] == thread_id)
            if effective["state_needs_review"] and not correction:
                raise StorageError("已有状态依据过期，请核查后使用 correction 修正。")
            if not correction and state not in TRANSITIONS[effective["state"]]:
                raise StorageError("该生命周期转换需要显式 correction 作者修正。")
            event = scope.event(event_id, allow_review=correction) if event_id else None
            if evidence_id:
                scope.evidence(evidence_id)
            transition = {"id": uid(), "thread_id": thread_id, "from_state": effective["state"], "to_state": state,
                "event_id": event, "evidence_id": evidence_id, "correction": int(correction), "reason": reason, "created_at": now()}
            conn.execute("INSERT INTO thread_transitions(id,thread_id,from_state,to_state,event_id,evidence_id,correction,reason,created_at) VALUES(:id,:thread_id,:from_state,:to_state,:event_id,:evidence_id,:correction,:reason,:created_at)", transition)
            conn.execute("UPDATE threads SET state=?,review_status='confirmed' WHERE id=?", (state, thread_id))
            if correction:
                conn.execute("UPDATE revision_reviews SET status='resolved',resolution=? WHERE project_id=? AND target_type='thread' AND target_id=? AND status='pending'",
                    ("作者重新确认伏笔状态：" + reason, project_id, thread_id))
            scope.touch("thread_state", thread_id, before, {**transition, "correction": correction}, reason)
            self.repo._refresh(conn, {"generation_ids": [before["generation_id"]]}, "thread")
            return transition
