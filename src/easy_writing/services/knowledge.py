"""按故事时点、正文边界和角色认知检索；未知不推断为没有发生。"""

import re
from collections import defaultdict

from easy_writing.services.timeline import TimelineService, event_root, reachable
from easy_writing.storage.database import StorageError


class KnowledgeService:
    def __init__(self, repo):
        self.repo = repo
        self.timeline = TimelineService(repo)

    def _catalog(self, conn, snap, *, current_only=False):
        visible = {}
        for generation in snap["generations"]:
            if current_only and generation in snap.get("quarantined_generations", ()):
                continue
            for row in conn.execute("SELECT e.* FROM entities e JOIN entity_mentions m ON m.entity_id=e.id WHERE m.generation_id=? AND e.review_status<>'rejected'", (generation,)):
                root = self.repo.canonical(conn, row["id"])
                if root["review_status"] == "rejected":
                    continue
                item = visible.setdefault(root["id"], {"id": root["id"], "name": row["name"], "kind": row["kind"], "aliases": set(), "source_ids": set()})
                item["source_ids"].add(row["id"])
                item["aliases"].add(row["name"])
                for alias in conn.execute("SELECT alias FROM entity_aliases WHERE entity_id=? AND (generation_id=? OR (author_confirmed=1 AND ?))", (row["id"], generation, snap["through_chapter"] is None)):
                    item["aliases"].add(alias[0])
                if row["id"] == root["id"]:
                    item["name"] = root["name"]
        return visible

    def _resolve(self, conn, snap, value):
        if value is None:
            return None
        direct = conn.execute("SELECT * FROM entities WHERE id=?", (value,)).fetchone()
        if direct:
            if direct["project_id"] != snap["project_id"]:
                raise StorageError("实体不属于当前项目。")
            root = self.repo.canonical(conn, value)["id"]
            if root in self._catalog(conn, snap):
                return root
        matches = [e["id"] for e in self._catalog(conn, snap).values() if value == e["name"] or value in e["aliases"]]
        if len(matches) != 1:
            raise StorageError("无法在可见范围内唯一匹配实体，请核对名称或使用明确的实体 ID。")
        return matches[0]

    def characters(self, project_id, *, through_chapter=None):
        with self.repo.db.connect() as conn:
            snap = self.timeline._snapshot(conn, project_id, through_chapter)
            rows = self._catalog(conn, snap).values()
            return [{"id": e["id"], "name": e["name"], "kind": e["kind"], "aliases": sorted(e["aliases"] - {e["name"]})} for e in rows if e["kind"] == "person"]

    @staticmethod
    def _compare(snap, a, b):
        if a is None or b is None or a not in snap["events"] or b not in snap["events"]:
            return "unknown"
        if a == b:
            return "equal"
        cache = snap.setdefault("_comparisons", {})
        if (a, b) in cache:
            return cache[a, b]
        adjacency = TimelineService._adjacency(snap)
        if reachable(adjacency, a, b):
            result = "before"
        elif reachable(adjacency, b, a):
            result = "after"
        else:
            result = "unknown"
        cache[a, b] = result
        cache[b, a] = {"before": "after", "after": "before", "unknown": "unknown"}[result]
        return result

    def _evidence(self, conn, record_id, snap):
        rows = conn.execute("""SELECT e.id,e.quote,e.quote_start,e.quote_end,s.paragraph_id,s.start_offset,
            v.id AS version_id,c.chapter_no FROM evidence_links e JOIN source_chunks s ON s.id=e.source_chunk_id
            JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id
            WHERE e.record_type='fact' AND e.record_id=? AND c.latest_version_id=v.id
            ORDER BY c.chapter_no,s.start_offset,e.quote_start""", (record_id,)).fetchall()
        return [{**dict(r), "absolute_start": r["start_offset"] + r["quote_start"]} for r in rows
                if snap["through_chapter"] is None or r["chapter_no"] <= snap["through_chapter"]]

    def _facts(self, conn, snap, *, subject=None, predicate=None, actor=None, at_event=None, confirmed_only=False):
        errors = self.timeline._validate(snap)
        if errors:
            raise StorageError("时间约束需要核查：" + "；".join(errors))
        catalog = self._catalog(conn, snap)
        subject_id = self._resolve(conn, snap, subject)
        actor_id = self._resolve(conn, snap, actor)
        point = event_root(conn, at_event, snap["through_chapter"]) if at_event else None
        if point and point not in snap["events"]:
            raise StorageError("查询时点不在可见的已发布事件范围。")
        result = []
        for generation in snap["generations"]:
            if generation in snap.get("quarantined_generations", ()):
                continue
            for row in conn.execute("SELECT * FROM facts WHERE generation_id=? AND review_status<>'rejected'", (generation,)):
                from easy_writing.storage.revisions import pending
                if pending(conn, "fact", row["id"]):
                    continue
                if confirmed_only and row["review_status"] != "confirmed":
                    continue
                sid = self.repo.canonical(conn, row["subject_id"])["id"]
                oid = self.repo.canonical(conn, row["object_entity_id"])["id"] if row["object_entity_id"] else None
                if sid not in catalog or (oid is not None and oid not in catalog):
                    continue
                if subject_id and sid != subject_id:
                    continue
                if predicate and row["predicate"] != predicate:
                    continue
                evidence = self._evidence(conn, row["id"], snap)
                if not evidence:
                    continue
                uncertain = []
                start = event_root(conn, row["effective_from_event_id"], snap["through_chapter"]) if row["effective_from_event_id"] else None
                end = event_root(conn, row["effective_until_event_id"], snap["through_chapter"]) if row["effective_until_event_id"] else None
                # 读者范围外的生效/结束事件既不参与判断，也不以 ID 泄露。
                if snap["through_chapter"] is not None:
                    start = start if start in snap["events"] else None
                    end = end if end in snap["events"] else None
                if point:
                    relation = self._compare(snap, start, point)
                    if relation == "after":
                        continue
                    if relation == "unknown":
                        uncertain.append("生效时间未能与查询时点确定排序")
                    if end:
                        relation = self._compare(snap, point, end)
                        if relation in ("equal", "after"):
                            continue
                        if relation == "unknown":
                            uncertain.append("结束时间未能确定排序")
                if actor_id:
                    holder = self.repo.canonical(conn, row["knowledge_holder_id"])["id"] if row["knowledge_holder_id"] else None
                    awareness = [a for a in conn.execute("SELECT * FROM fact_awareness WHERE fact_id=?", (row["id"],))
                                 if self.repo.canonical(conn, a["actor_id"])["id"] == actor_id]
                    awareness = [a for a in awareness if snap["through_chapter"] is None or (a["revealed_at_chapter"] is not None and a["revealed_at_chapter"] <= snap["through_chapter"])]
                    if holder != actor_id and not awareness:
                        continue
                    if awareness:
                        learned = event_root(conn, awareness[0]["learned_event_id"], snap["through_chapter"])
                    else:
                        learned = start
                    if point:
                        relation = self._compare(snap, learned, point)
                        if relation == "after":
                            continue
                        if relation == "unknown":
                            uncertain.append("角色获知时间未能确定排序")
                mode = conn.execute("SELECT mode FROM fact_modes WHERE fact_id=?", (row["id"],)).fetchone()
                result.append({**dict(row), "subject_id": sid, "subject_name": catalog[sid]["name"],
                               "effective_from_event_id": start, "effective_until_event_id": end,
                               "object_entity_id": oid, "object_name": catalog[oid]["name"] if oid else row["object_value"],
                               "mode": mode[0] if mode else "assertion", "effective_from": start, "effective_until": end,
                               "time_uncertain": bool(uncertain), "uncertainties": uncertain, "evidence": evidence})
        # 只有作者声明为单值状态的命题才采用“已确定更晚的生效状态覆盖早期状态”。
        if point:
            result = [fact for fact in result if not any(
                fact["id"] != other["id"] and fact["mode"] == other["mode"] == "state"
                and fact["subject_id"] == other["subject_id"] and fact["predicate"] == other["predicate"]
                and fact["knowledge_holder_id"] == other["knowledge_holder_id"] and not other["time_uncertain"]
                and (fact["review_status"] != "confirmed" or other["review_status"] == "confirmed")
                and self._compare(snap, fact["effective_from"], other["effective_from"]) == "before"
                for other in result)]
        return result

    def facts(self, project_id, *, subject=None, predicate=None, actor=None, at_event=None, through_chapter=None, confirmed_only=False, limit=100, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise StorageError("查询分页参数无效。")
        with self.repo.db.connect() as conn:
            snap = self.timeline._snapshot(conn, project_id, through_chapter)
            rows = self._facts(conn, snap, subject=subject, predicate=predicate, actor=actor, at_event=at_event, confirmed_only=confirmed_only)
            coverage = self._coverage(conn, snap)
            return {"facts": rows[offset:offset + limit], "total": len(rows), "snapshot_id": snap["snapshot_id"],
                    "through_chapter": through_chapter, "at_event": at_event, "actor": actor,
                    "coverage": coverage, "absence_means": "未找到可证明的记录，不等于角色不知道或事件未发生"}

    @staticmethod
    def _coverage(conn, snap):
        rows = conn.execute("""SELECT c.chapter_no,c.latest_version_id,j.version_id FROM chapters c
            LEFT JOIN ingestion_jobs j ON j.id=c.published_generation_id WHERE c.project_id=? ORDER BY c.chapter_no""", (snap["project_id"],)).fetchall()
        rows = [r for r in rows if snap["through_chapter"] is None or r["chapter_no"] <= snap["through_chapter"]]
        loaded = sorted(c["chapter_no"] for c in snap["generations"].values())
        boundary = snap["through_chapter"] or max((r["chapter_no"] for r in rows), default=0)
        known, gaps, previous = sorted({r["chapter_no"] for r in rows}), [], 0
        for number in [*known, boundary + 1]:
            if number > previous + 1:
                gaps.append({"start": previous + 1, "end": number - 1})
            previous = number
        current_generations = set(snap["generations"])
        pending_count = 0
        for review in conn.execute("SELECT target_type,target_id FROM revision_reviews WHERE project_id=? AND status='pending' GROUP BY target_type,target_id", (snap["project_id"],)):
            if review["target_type"] in ("fact", "event", "thread", "summary", "link"):
                from easy_writing.storage.repository import RECORD_TABLES
                target = conn.execute(f"SELECT generation_id FROM {RECORD_TABLES[review['target_type']]} WHERE id=?", (review["target_id"],)).fetchone()
                pending_count += bool(target and target[0] in current_generations)
        return {"loaded_chapters": loaded, "pending_review_count": pending_count,
                "quarantined_chapters": sorted(c["chapter_no"] for g,c in snap["generations"].items() if g in snap.get("quarantined_generations", ())),
                "not_current": [r["chapter_no"] for r in rows if r["latest_version_id"] != r["version_id"]],
                "missing_chapter_ranges": gaps, "through_chapter": snap["through_chapter"], "full_book_claim": False}

    def relations(self, project_id, *, through_chapter=None, at_event=None):
        with self.repo.db.connect() as conn:
            snap = self.timeline._snapshot(conn, project_id, through_chapter)
            catalog = self._catalog(conn, snap)
            facts = self._facts(conn, snap, at_event=at_event)
            return {"relations": [f for f in facts if f["object_entity_id"] and catalog[f["subject_id"]]["kind"] == catalog[f["object_entity_id"]]["kind"] == "person"],
                    "snapshot_id": snap["snapshot_id"]}

    def bind_fact(self, fact_id, *, from_event=None, until_event=None, mode="state", reason):
        self.timeline._reason(reason)
        if mode not in ("state", "assertion"):
            raise StorageError("事实模式应为 state 或 assertion。")
        impact = self.repo.impact("fact", fact_id)
        with self.repo.db.transaction() as conn:
            before = dict(self.repo.require(conn, "facts", fact_id))
            snap = self.timeline._snapshot(conn, impact["project_id"])
            if before["generation_id"] not in snap["generations"]:
                raise StorageError("事实已经过期。")
            anchors = []
            for value in (from_event, until_event):
                root = event_root(conn, value) if value else None
                if root and root not in snap["events"]:
                    raise StorageError("事实生效事件必须属于同一项目的当前知识。")
                anchors.append(root)
            if all(anchors) and self._compare(snap, anchors[0], anchors[1]) in ("equal", "after"):
                raise StorageError("事实结束事件必须晚于生效事件；不明确时保留相对约束。")
            conn.execute("UPDATE facts SET effective_from_event_id=?,effective_until_event_id=? WHERE id=?", (*anchors, fact_id))
            conn.execute("INSERT INTO fact_modes VALUES(?,?) ON CONFLICT(fact_id) DO UPDATE SET mode=excluded.mode", (fact_id, mode))
            after = {**dict(self.repo.require(conn, "facts", fact_id)), "mode": mode}
            self.repo._audit(conn, impact["project_id"], "fact_context", fact_id, before, after, reason)
            self.repo._refresh(conn, impact, "fact")
            self.timeline._touch(conn, impact["project_id"])
            return after

    def learn(self, fact_id, actor, learned_event, *, reason, revealed_at_chapter=None):
        self.timeline._reason(reason)
        self.timeline._reveal(revealed_at_chapter)
        impact = self.repo.impact("fact", fact_id)
        with self.repo.db.transaction() as conn:
            snap = self.timeline._snapshot(conn, impact["project_id"])
            fact = self.repo.require(conn, "facts", fact_id)
            if fact["generation_id"] not in snap["generations"]:
                raise StorageError("事实不在当前知识中。")
            actor_id = self._resolve(conn, snap, actor)
            root = event_root(conn, learned_event)
            if root not in snap["events"]:
                raise StorageError("角色获知事件必须属于当前项目。")
            old = conn.execute("SELECT * FROM fact_awareness WHERE fact_id=? AND actor_id=?", (fact_id, actor_id)).fetchone()
            conn.execute("INSERT INTO fact_awareness VALUES(?,?,?,?) ON CONFLICT(fact_id,actor_id) DO UPDATE SET learned_event_id=excluded.learned_event_id,revealed_at_chapter=excluded.revealed_at_chapter", (fact_id, actor_id, root, revealed_at_chapter))
            after = {"fact_id": fact_id, "actor_id": actor_id, "learned_event_id": root, "revealed_at_chapter": revealed_at_chapter}
            self.repo._audit(conn, impact["project_id"], "fact_awareness", fact_id, dict(old) if old else {}, after, reason)
            self.timeline._touch(conn, impact["project_id"])
            return after

    @staticmethod
    def _chapter_number(text):
        matches = re.findall(r"第([0-9零〇一二两三四五六七八九十百千]+)章", text)
        if len(set(matches)) > 1:
            raise StorageError("一次查询请使用单一章节边界；跨章节比较可分别查询。")
        match = re.search(r"第([0-9零〇一二两三四五六七八九十百千]+)章", text)
        if not match:
            if re.search(r"第[^\s，。！？]{1,20}章", text):
                raise StorageError("无法识别章节表达，请使用阿拉伯数字或 --chapter 明确范围。")
            return None
        value = match[1]
        if value.isdigit():
            number = int(value)
        else:
            digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
            digits.update({"〇": 0, "两": 2})
            result, current = 0, 0
            for char in value:
                if char in digits:
                    current = digits[char]
                else:
                    result += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                    current = 0
            number = result + current
        if re.match(r"之前|以前|前", text[match.end():]):
            number -= 1
        if number <= 0:
            raise StorageError("该表达未包含有效的正文章节范围。")
        return number

    def answer(self, project_id, question, *, through_chapter=None, subject=None, predicate=None, actor=None, at_event=None, limit=10):
        if not question.strip() or not 1 <= limit <= 50:
            raise StorageError("请输入问题，并将结果预算限制在 1～50 条。")
        embedded = self._chapter_number(question)
        if embedded is not None and through_chapter is not None and embedded != through_chapter:
            raise StorageError("问题中的章节与 --chapter 不一致。")
        through_chapter = through_chapter if through_chapter is not None else embedded
        with self.repo.db.connect() as conn:
            snap = self.timeline._snapshot(conn, project_id, through_chapter)
            catalog = self._catalog(conn, snap)
            if actor is None and re.search(r"知道|知晓|获知|知情", question):
                actors = [e for e in catalog.values() if e["kind"] == "person" and any(
                    name and re.search(re.escape(name) + r".{0,8}(知道|知晓|获知|知情)", question)
                    for name in e["aliases"] | {e["name"]})]
                if len(actors) != 1:
                    raise StorageError("认知问题请通过 --actor 明确指定角色。")
                actor = actors[0]["id"]
            if subject is None:
                matches = [e for e in catalog.values() if any(name and name in question for name in e["aliases"] | {e["name"]})]
                # 优先检索非人物对象（如钥匙状态）；多对象时返回相关命题而不猜唯一主体。
                objects = [e for e in matches if e["kind"] != "person"]
                if len(objects) == 1:
                    subject = objects[0]["id"]
                elif len(matches) == 1 and actor is None:
                    subject = matches[0]["id"]
            rows = self._facts(conn, snap, subject=subject, predicate=predicate, actor=actor, at_event=at_event)
            if subject is None and predicate is None:
                terms = set(re.findall(r"[A-Za-z0-9_]{2,}", question))
                for group in re.findall(r"[\u4e00-\u9fff]+", question):
                    terms.update(group[i:i+2] for i in range(len(group) - 1))
                def score(f):
                    text = f["subject_name"] + f["predicate"] + f["object_name"]
                    return sum(term in text for term in terms)
                rows = sorted((f for f in rows if score(f)), key=score, reverse=True)
            selected = rows[:limit]
            answer = "未找到足够的可见知识依据，不能据此断言事件未发生或角色不知情。"
            if selected:
                answer = "；".join(f"{f['subject_name']}／{f['predicate']}：{f['object_name']}" + ("（时间未定）" if f["time_uncertain"] else "") for f in selected)
                answer += "。以上为已记录命题；未确认或推测项仍需作者核对。"
            return {"answer": answer, "facts": selected, "matched_total": len(rows), "truncated": len(rows) > limit,
                    "snapshot_id": snap["snapshot_id"], "through_chapter": through_chapter,
                    "coverage": self._coverage(conn, snap), "model_calls": 0}
