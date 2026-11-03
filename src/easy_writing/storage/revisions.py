"""事务内的依赖传播与保守标注迁移；不调用模型。"""

import json
from collections import defaultdict, deque

from easy_writing.storage.repository import RECORD_TABLES, now, uid


def pending(conn, kind, record_id):
    return conn.execute("SELECT 1 FROM revision_reviews WHERE target_type=? AND target_id=? AND status='pending' LIMIT 1", (kind, record_id)).fetchone() is not None


def pending_generations(conn, project):
    generations = set()
    for kind in ("fact", "event", "thread", "summary", "link"):
        generations.update(r[0] for r in conn.execute(f"""SELECT DISTINCT t.generation_id FROM {RECORD_TABLES[kind]} t
            JOIN revision_reviews r ON r.target_id=t.id AND r.target_type=? WHERE r.project_id=? AND r.status='pending'""", (kind, project)))
    return generations


def queue(conn, project, change, origin, target, reason):
    conn.execute("INSERT OR IGNORE INTO revision_reviews VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), project, change, *origin, *target, "pending", reason, None))


def changed(conn, project, kind, record_id, details=None, *, origins=()):
    change = uid()
    conn.execute("INSERT INTO revision_changes VALUES(?,?,?,?,?,?)", (change, project, kind, record_id, json.dumps(details or {}, ensure_ascii=False), now()))
    adjacency = defaultdict(list)
    for edge in conn.execute("SELECT * FROM record_dependencies WHERE project_id=?", (project,)):
        adjacency[edge["source_type"], edge["source_id"]].append((edge["target_type"], edge["target_id"]))
    if kind == "chapter_rewrite":
        # 数据库已经明确表达的锚点依赖，无需作者重复登记；不猜测任意句子的语义。
        for row in conn.execute("""SELECT f.* FROM facts f JOIN ingestion_jobs j ON j.id=f.generation_id
            JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id WHERE c.project_id=?""", (project,)):
            for anchor in (row["effective_from_event_id"], row["effective_until_event_id"]):
                if anchor:
                    adjacency["event", anchor].append(("fact", row["id"]))
            for a in conn.execute("SELECT learned_event_id FROM fact_awareness WHERE fact_id=?", (row["id"],)):
                adjacency["event", a[0]].append(("fact", row["id"]))
        for row in conn.execute("SELECT * FROM knowledge_uses WHERE project_id=? AND enabled=1", (project,)):
            adjacency["fact", row["fact_id"]].append(("event", row["event_id"]))
        for row in conn.execute("""SELECT t.* FROM thread_transitions t JOIN threads f ON f.id=t.thread_id
            JOIN ingestion_jobs j ON j.id=f.generation_id JOIN chapter_versions v ON v.id=j.version_id
            JOIN chapters c ON c.id=v.chapter_id WHERE c.project_id=? AND t.event_id IS NOT NULL""", (project,)):
            adjacency["event", row["event_id"]].append(("thread", row["thread_id"]))
    for origin in origins or [(kind, record_id)]:
        seen, work = {origin}, deque(adjacency[origin])
        while work:
            target = work.popleft()
            if target in seen:
                continue
            seen.add(target)
            queue(conn, project, change, origin, target, "上游知识或作者标注变化，需重新核对当前记录。")
            if target[0] in RECORD_TABLES and target[0] != "entity":
                row = conn.execute(f"SELECT generation_id FROM {RECORD_TABLES[target[0]]} WHERE id=?", (target[1],)).fetchone()
                if row:
                    conn.execute("UPDATE summaries SET is_stale=1 WHERE generation_id=?", (row[0],))
            work.extend(adjacency[target])
    return change


def chapter_revised(conn, project, chapter_id, previous_version, next_version, previous_generation):
    origins = []
    if previous_generation:
        for kind in ("fact", "event", "thread", "summary", "link"):
            origins.extend((kind, r[0]) for r in conn.execute(f"SELECT id FROM {RECORD_TABLES[kind]} WHERE generation_id=?", (previous_generation,)))
    change = changed(conn, project, "chapter_rewrite", chapter_id,
        {"from_version": previous_version, "to_version": next_version, "previous_generation": previous_generation}, origins=origins)
    number = conn.execute("SELECT chapter_no FROM chapters WHERE id=?", (chapter_id,)).fetchone()[0]
    # 未声明的跨章语义依赖无法精确识别：保守失效后文概览，不擅自否决其原文事实。
    conn.execute("""UPDATE summaries SET is_stale=1 WHERE generation_id IN
        (SELECT published_generation_id FROM chapters WHERE project_id=? AND chapter_no>=?)""", (project, number))
    return change


def _signature(conn, kind, row):
    evidence = [tuple(r) for r in conn.execute("""SELECT s.text,e.quote,e.quote_start,e.quote_end FROM evidence_links e
        JOIN source_chunks s ON s.id=e.source_chunk_id WHERE e.record_type=? AND e.record_id=?
        ORDER BY s.text,e.quote,e.quote_start,e.quote_end""", (kind, row["id"]))]
    if not evidence:
        return None
    if kind == "event":
        fields = [row[k] for k in ("title", "summary", "story_time")]
        fields += [sorted(r[0] for r in conn.execute("SELECT entity_id FROM event_entities WHERE event_id=?", (row["id"],)))]
    elif kind == "fact":
        fields = [row[k] for k in ("subject_id", "predicate", "object_value", "object_entity_id", "knowledge_holder_id", "certainty")]
    else:
        fields = [row["title"], row["description"]]
    return json.dumps([fields, evidence], ensure_ascii=False, sort_keys=True)


def align_generation(conn, project, old_generation, new_generation):
    if not old_generation or old_generation == new_generation:
        return {"matched": 0, "review_required": 0}
    change = changed(conn, project, "chapter_publication", new_generation, {"old_generation": old_generation})
    maps, old_rows, new_rows = {}, {}, {}
    for kind in ("event", "fact", "thread"):
        table = RECORD_TABLES[kind]
        old_rows[kind] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE generation_id=?", (old_generation,))]
        new_rows[kind] = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE generation_id=?", (new_generation,))]
        left, right = defaultdict(list), defaultdict(list)
        for row in old_rows[kind]:
            left[_signature(conn, kind, row)].append(row["id"])
        for row in new_rows[kind]:
            right[_signature(conn, kind, row)].append(row["id"])
        for signature in left.keys() & right.keys():
            if signature is not None and len(left[signature]) == len(right[signature]) == 1:
                old, new = left[signature][0], right[signature][0]
                maps[kind, old] = new
                conn.execute("INSERT OR IGNORE INTO record_successions VALUES(?,?,?,?,?)", (project, kind, old, new, change))
                status = next(r["review_status"] for r in old_rows[kind] if r["id"] == old)
                conn.execute(f"UPDATE {table} SET review_status=? WHERE id=?", (status, new))

    current_generations = {r[0] for r in conn.execute("""SELECT c.published_generation_id FROM chapters c
        JOIN ingestion_jobs j ON j.id=c.published_generation_id WHERE c.project_id=? AND c.latest_version_id=j.version_id""", (project,))}
    current_generations.add(new_generation)
    def event(value):
        if not value:
            return None
        if ("event", value) in maps:
            return maps["event", value]
        # 既有身份对齐只沿已保存映射解析；旧代事件不凭标题猜测。
        from easy_writing.services.timeline import event_root
        root = event_root(conn, value)
        row = conn.execute("SELECT generation_id FROM events WHERE id=? AND review_status<>'rejected'", (root,)).fetchone()
        if not row or row[0] not in current_generations or row[0] == old_generation:
            raise ValueError("event_anchor_expired")
        return root

    for (kind, old), new in list(maps.items()):
        if kind == "event":
            old_identity = conn.execute("SELECT * FROM event_identity WHERE event_id=?", (old,)).fetchone()
            chapter_no = conn.execute("""SELECT c.chapter_no FROM ingestion_jobs j JOIN chapter_versions v ON v.id=j.version_id
                JOIN chapters c ON c.id=v.chapter_id WHERE j.id=?""", (new_generation,)).fetchone()[0]
            conn.execute("INSERT INTO event_identity VALUES(?,?,?) ON CONFLICT(event_id) DO UPDATE SET target_id=excluded.target_id,revealed_at_chapter=excluded.revealed_at_chapter", (old, new, chapter_no))
            if old_identity:
                try:
                    target = event(old_identity["target_id"])
                    if target != new:
                        conn.execute("INSERT OR REPLACE INTO event_identity VALUES(?,?,?)", (new, target, old_identity["revealed_at_chapter"]))
                except ValueError:
                    queue(conn, project, change, ("event", old), ("event", new), "原事件身份对应的目标已变，需作者重新对齐。")
            annotation = conn.execute("SELECT * FROM event_times WHERE event_id=?", (old,)).fetchone()
            if annotation:
                conn.execute("INSERT OR REPLACE INTO event_times VALUES(?,?,?,?,?)", (new, annotation["start_tick"], annotation["end_tick"], annotation["frame"], annotation["revealed_at_chapter"]))
    # 证据本身也必须唯一匹配；句子相同但所属段落改变，不复制作者确认。
    evidence_map = {}
    for (kind, old), new in maps.items():
        for ref in conn.execute("SELECT * FROM evidence_links WHERE record_type=? AND record_id=?", (kind, old)):
            matches = conn.execute("""SELECT n.id FROM evidence_links n JOIN source_chunks ns ON ns.id=n.source_chunk_id
                JOIN source_chunks os ON os.id=? WHERE n.record_type=? AND n.record_id=? AND n.quote=?
                AND ns.text=os.text AND n.quote_start=? AND n.quote_end=?""", (ref["source_chunk_id"], kind, new, ref["quote"], ref["quote_start"], ref["quote_end"])).fetchall()
            if len(matches) == 1:
                evidence_map[ref["id"]] = matches[0][0]
    def evidence(value):
        if not value:
            return None
        if value in evidence_map:
            return evidence_map[value]
        visible = conn.execute("""SELECT c.latest_version_id=s.version_id FROM evidence_links e JOIN source_chunks s ON s.id=e.source_chunk_id
            JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id WHERE e.id=? AND c.project_id=?""", (value, project)).fetchone()
        if not visible or not visible[0]:
            raise ValueError("source_expired")
        return value

    for old in old_rows["fact"]:
        new = maps.get(("fact", old["id"]))
        if not new:
            continue
        try:
            start, end = event(old["effective_from_event_id"]), event(old["effective_until_event_id"])
            awareness = [(a, event(a["learned_event_id"])) for a in conn.execute("SELECT * FROM fact_awareness WHERE fact_id=?", (old["id"],))]
        except ValueError:
            queue(conn, project, change, ("fact", old["id"]), ("fact", new), "原事实文字未变，但生效 / 获知事件改变，作者标注需重审。")
            continue
        conn.execute("UPDATE facts SET effective_from_event_id=?,effective_until_event_id=? WHERE id=?", (start, end, new))
        mode = conn.execute("SELECT mode FROM fact_modes WHERE fact_id=?", (old["id"],)).fetchone()
        if mode:
            conn.execute("INSERT OR REPLACE INTO fact_modes VALUES(?,?)", (new, mode[0]))
        for a, learned in awareness:
            conn.execute("INSERT OR REPLACE INTO fact_awareness VALUES(?,?,?,?)", (new, a["actor_id"], learned, a["revealed_at_chapter"]))
    for old in old_rows["thread"]:
        new = maps.get(("thread", old["id"]))
        if not new:
            continue
        try:
            transitions = [(dict(t), event(t["event_id"]), evidence(t["evidence_id"])) for t in conn.execute("SELECT * FROM thread_transitions WHERE thread_id=? ORDER BY seq", (old["id"],))]
        except ValueError:
            queue(conn, project, change, ("thread", old["id"]), ("thread", new), "伏笔状态的关联事件或证据已改变，回收状态需重审。")
            continue
        for t, anchor, ref in transitions:
            conn.execute("""INSERT INTO thread_transitions(id,thread_id,from_state,to_state,event_id,evidence_id,correction,reason,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (uid(), new, t["from_state"], t["to_state"], anchor, ref, t["correction"], "证据未变自动沿用：" + t["reason"], t["created_at"]))
        conn.execute("UPDATE threads SET state=? WHERE id=?", (old["state"], new))
    # 规则、使用标注保留原 ID 与原因，只对可以证明同一含义的端点改绑。
    for table, fact_column in (("world_constraints", "rule_fact_id"), ("knowledge_uses", "fact_id")):
        for row in conn.execute(f"SELECT * FROM {table} WHERE project_id=?", (project,)).fetchall():
            replacement = maps.get(("fact", row[fact_column]))
            fields = {fact_column: replacement} if replacement else {}
            try:
                for column in (("from_event_id", "until_event_id") if table == "world_constraints" else ("event_id",)):
                    fields[column] = event(row[column])
                if table == "knowledge_uses":
                    fields["evidence_id"] = evidence(row["evidence_id"])
            except ValueError:
                continue
            if fields:
                conn.execute(f"UPDATE {table} SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), row["id"]))
    for link in conn.execute("SELECT * FROM event_links WHERE generation_id=?", (old_generation,)).fetchall():
        try:
            source, target = event(link["from_event_id"]), event(link["to_event_id"])
        except ValueError:
            continue
        existing = conn.execute("SELECT id FROM event_links WHERE generation_id=? AND from_event_id=? AND to_event_id=? AND relation_type=?", (new_generation, source, target, link["relation_type"])).fetchone()
        if existing:
            continue
        link_id = uid()
        conn.execute("INSERT INTO event_links VALUES(?,?,?,?,?,?)", (link_id, new_generation, source, target, link["relation_type"], link["review_status"]))
        visibility = conn.execute("SELECT revealed_at_chapter FROM link_visibility WHERE link_id=?", (link["id"],)).fetchone()
        if visibility:
            conn.execute("INSERT INTO link_visibility VALUES(?,?)", (link_id, visibility[0]))
    for edge in conn.execute("SELECT * FROM record_dependencies WHERE project_id=?", (project,)).fetchall():
        source = maps.get((edge["source_type"], edge["source_id"]), edge["source_id"])
        target = maps.get((edge["target_type"], edge["target_id"]), edge["target_id"])
        if (source, target) != (edge["source_id"], edge["target_id"]):
            conn.execute("INSERT OR IGNORE INTO record_dependencies VALUES(?,?,?,?,?,?,?)", (uid(), project, edge["source_type"], source, edge["target_type"], target, "证据未变自动沿用：" + edge["reason"]))
    # 未匹配的原有作者决定不消失，作为显式重审项保留。
    for kind in ("event", "fact", "thread"):
        for old in old_rows[kind]:
            if (kind, old["id"]) not in maps and (old["review_status"] != "unconfirmed" or conn.execute("SELECT 1 FROM edit_history WHERE record_id=? LIMIT 1", (old["id"],)).fetchone()):
                queue(conn, project, change, (kind, old["id"]), (kind, old["id"]), "原作者标注未能唯一对齐新稿；保留历史，请对照新稿重新设置。")
    # 发布前保守隔离的依赖，只有根依据完全未变才可自动解除。
    for (kind, old), new in maps.items():
        if not pending(conn, kind, new):
            conn.execute("""UPDATE revision_reviews SET status='resolved',resolution='依据与原文段落唯一匹配，自动改绑后解除隔离'
                WHERE project_id=? AND source_type=? AND source_id=? AND target_id<>? AND status='pending'
                AND change_id IN (SELECT id FROM revision_changes WHERE kind='chapter_rewrite')""", (project, kind, old, old))
    return {"matched": len(maps), "review_required": conn.execute("SELECT COUNT(*) FROM revision_reviews WHERE change_id=? AND status='pending'", (change,)).fetchone()[0]}
