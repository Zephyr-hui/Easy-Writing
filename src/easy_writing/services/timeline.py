"""事件网络：明确的时间约束、可见范围与无损的身份对齐。"""

import json
import math
from collections import defaultdict, deque

from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import Repository, digest, uid

RELATIONS = {"before", "overlaps", "during", "causes", "foreshadows", "resolves"}


def event_root(conn, event_id, through_chapter=None):
    seen = set()
    while True:
        if event_id in seen:
            raise StorageError("事件身份映射存在环。")
        seen.add(event_id)
        row = conn.execute("SELECT * FROM event_identity WHERE event_id=?", (event_id,)).fetchone()
        if row is None or (through_chapter is not None and (row["revealed_at_chapter"] is None or row["revealed_at_chapter"] > through_chapter)):
            return event_id
        event_id = row["target_id"]


def reachable(adjacency, source, target):
    pending, seen = [source], set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    return False


def layers_for(nodes, adjacency):
    incoming = dict.fromkeys(nodes, 0)
    for source, targets in adjacency.items():
        for target in targets:
            incoming[target] += 1
    levels, remaining = [], set(nodes)
    while remaining:
        group = sorted(n for n in remaining if incoming[n] == 0)
        if not group:
            raise StorageError("严格时间先后关系存在环。")
        levels.append(group)
        for node in group:
            remaining.remove(node)
            for target in adjacency.get(node, ()):
                incoming[target] -= 1
    return levels


class TimelineService:
    def __init__(self, repo: Repository):
        self.repo = repo

    @staticmethod
    def _reason(reason):
        if not reason.strip():
            raise StorageError("作者修改需要填写原因。")

    @staticmethod
    def _reveal(chapter):
        if chapter is not None and (type(chapter) is not int or chapter <= 0):
            raise StorageError("揭示章节必须为正整数，或留空表示仅作者可见。")

    def _project_for_event(self, conn, event_id):
        row = conn.execute("""SELECT c.project_id FROM events e JOIN ingestion_jobs j ON j.id=e.generation_id
            JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id WHERE e.id=?""", (event_id,)).fetchone()
        if row is None:
            raise StorageError("事件不存在。")
        return row[0]

    @staticmethod
    def _touch(conn, project_id):
        conn.execute("INSERT INTO graph_revisions VALUES(?,1) ON CONFLICT(project_id) DO UPDATE SET revision=revision+1", (project_id,))

    def _snapshot(self, conn, project_id, through_chapter=None):
        self._reveal(through_chapter)
        if not conn.in_transaction:
            conn.execute("BEGIN")
        self.repo.require(conn, "projects", project_id)
        chapters = self.repo.visible(conn, project_id)
        if through_chapter is not None:
            chapters = [c for c in chapters if c["chapter_no"] <= through_chapter]
        generations = {c["published_generation_id"]: c for c in chapters}
        from easy_writing.storage.revisions import pending_generations
        quarantined = pending_generations(conn, project_id)
        events, mentions, raw_to_root, raw_events = {}, [], {}, {}
        for generation, chapter in generations.items():
            if generation in quarantined:
                continue
            rows = conn.execute("SELECT * FROM events WHERE generation_id=? AND review_status<>'rejected'", (generation,)).fetchall()
            for row in rows:
                from easy_writing.storage.revisions import pending
                if pending(conn, "event", row["id"]):
                    continue
                raw_events[row["id"]] = dict(row)
                root = event_root(conn, row["id"], through_chapter)
                if self.repo.require(conn, "events", root)["review_status"] == "rejected":
                    continue
                raw_to_root[row["id"]] = root
                event = events.setdefault(root, {**dict(row), "id": root, "source_event_ids": [], "mentions": []})
                event["source_event_ids"].append(row["id"])
                for m in conn.execute("""SELECT m.id,m.mention_kind,s.paragraph_id,s.scene_no,s.start_offset,
                    COALESCE((SELECT MIN(e.quote_start) FROM evidence_links e WHERE e.record_type='event'
                        AND e.record_id=m.event_id AND e.source_chunk_id=m.source_chunk_id),0) AS quote_start
                    FROM event_mentions m JOIN source_chunks s ON s.id=m.source_chunk_id
                    WHERE m.event_id=? AND s.version_id=?""", (row["id"], chapter["knowledge_version_id"])):
                    item = {**dict(m), "event_id": root, "source_event_id": row["id"], "chapter_no": chapter["chapter_no"], "offset": m["start_offset"] + m["quote_start"], "title": row["title"]}
                    event["mentions"].append(item)
                    mentions.append(item)
        times = {}
        for root, event in events.items():
            # 不从后文 canonical 记录借用标题或正文描述。
            event["mentions"].sort(key=lambda m: (m["chapter_no"], m["offset"], m["id"]))
            if event["mentions"]:
                visible_raw = root if root in raw_events else event["mentions"][0]["source_event_id"]
                for field in ("title", "summary", "story_time", "review_status"):
                    event[field] = raw_events[visible_raw][field]
            annotation = conn.execute("SELECT * FROM event_times WHERE event_id=?", (root,)).fetchone()
            if annotation and (through_chapter is None or (annotation["revealed_at_chapter"] is not None and annotation["revealed_at_chapter"] <= through_chapter)):
                times[root] = dict(annotation)
            event["time"] = times.get(root)
        links = []
        for row in conn.execute("""SELECT l.* FROM event_links l JOIN ingestion_jobs j ON j.id=l.generation_id
            JOIN chapter_versions v ON v.id=j.version_id JOIN chapters c ON c.id=v.chapter_id
            WHERE c.project_id=? AND c.published_generation_id=j.id AND c.latest_version_id=v.id""", (project_id,)):
            if row["generation_id"] in quarantined:
                continue
            source = event_root(conn, row["from_event_id"], through_chapter)
            target = event_root(conn, row["to_event_id"], through_chapter)
            if source not in events or target not in events:
                continue
            if through_chapter is not None:
                visibility = conn.execute("SELECT revealed_at_chapter FROM link_visibility WHERE link_id=?", (row["id"],)).fetchone()
                if visibility is not None:
                    reveal = visibility[0]
                else:
                    reveal = conn.execute("""SELECT MAX(c.chapter_no) FROM evidence_links e JOIN source_chunks s ON s.id=e.source_chunk_id
                        JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id
                        WHERE e.record_type='link' AND e.record_id=? AND c.latest_version_id=v.id""", (row["id"],)).fetchone()[0]
                if reveal is None or reveal > through_chapter:
                    continue
            links.append({**dict(row), "source": source, "target": target})
        mentions.sort(key=lambda m: (m["chapter_no"], m["offset"], m["id"]))
        revision = conn.execute("SELECT revision FROM graph_revisions WHERE project_id=?", (project_id,)).fetchone()
        edits = conn.execute("SELECT COUNT(*),MAX(created_at) FROM edit_history WHERE project_id=?", (project_id,)).fetchone()
        all_chapters = conn.execute("SELECT id,latest_version_id,published_generation_id FROM chapters WHERE project_id=? ORDER BY chapter_no", (project_id,)).fetchall()
        snapshot_id = digest(json.dumps([list(tuple(c) for c in all_chapters), revision[0] if revision else 0, tuple(edits), through_chapter]))
        return {"events": events, "mentions": mentions, "links": links, "times": times,
                "generations": generations, "raw_to_root": raw_to_root, "snapshot_id": snapshot_id,
                "through_chapter": through_chapter, "project_id": project_id, "quarantined_generations": quarantined}

    @staticmethod
    def _adjacency(snapshot, include_numeric=True):
        cache_key = "_numeric_adjacency" if include_numeric else "_before_adjacency"
        if cache_key in snapshot:
            return snapshot[cache_key]
        adjacency = {node: set() for node in snapshot["events"]}
        for link in snapshot["links"]:
            if link["review_status"] == "confirmed" and link["relation_type"] == "before":
                adjacency[link["source"]].add(link["target"])
        if include_numeric:
            for a, left in snapshot["times"].items():
                for b, right in snapshot["times"].items():
                    if a != b and left["frame"] == right["frame"] and left["end_tick"] is not None and right["start_tick"] is not None and left["end_tick"] < right["start_tick"]:
                        adjacency[a].add(b)
        snapshot[cache_key] = adjacency
        return adjacency

    def _validate(self, snapshot):
        adjacency = self._adjacency(snapshot, include_numeric=False)
        errors = []
        try:
            levels = layers_for(snapshot["events"], adjacency)
        except StorageError:
            return ["严格先后关系形成环，或事件对齐后出现先于自身。"]
        times = snapshot["times"]
        bounds = {node: {} for node in adjacency}
        # 沿拓扑层传播祖先时间边界，避免对每一对事件重复遍历图。
        for level in levels:
            for node in level:
                annotation = times.get(node)
                inherited = dict(bounds[node])
                if annotation:
                    if any(frame != annotation["frame"] for frame in inherited):
                        errors.append("严格先后关系跨越不同时间参照系。")
                    minimum = inherited.get(annotation["frame"])
                    if minimum is not None and annotation["start_tick"] is not None and minimum >= annotation["start_tick"]:
                        errors.append("严格先后路径与已知时间范围冲突。")
                    bound = annotation["end_tick"]
                    old = inherited.get(annotation["frame"])
                    inherited[annotation["frame"]] = max(old, bound) if old is not None and bound is not None else old if bound is None else bound
                for child in adjacency[node]:
                    for frame, bound in inherited.items():
                        old = bounds[child].get(frame)
                        bounds[child][frame] = max(old, bound) if old is not None and bound is not None else old if bound is None else bound
        for link in snapshot["links"]:
            if link["review_status"] != "confirmed":
                continue
            left, right = times.get(link["source"]), times.get(link["target"])
            if not left or not right or link["relation_type"] not in ("during", "overlaps"):
                continue
            if left["frame"] != right["frame"]:
                errors.append("时间区间关系跨越不同参照系。")
                continue
            if None in (left["start_tick"], left["end_tick"], right["start_tick"], right["end_tick"]):
                continue
            if link["relation_type"] == "during" and not (right["start_tick"] <= left["start_tick"] <= left["end_tick"] <= right["end_tick"]):
                errors.append("包含关系与时间范围冲突。")
            if link["relation_type"] == "overlaps" and max(left["start_tick"], right["start_tick"]) > min(left["end_tick"], right["end_tick"]):
                errors.append("重叠关系与时间范围冲突。")
        return sorted(set(errors))

    def timeline(self, project_id, *, order="story", through_chapter=None):
        if order not in ("story", "narrative"):
            raise StorageError("时间线类型应为 story 或 narrative。")
        with self.repo.db.connect() as conn:
            snap = self._snapshot(conn, project_id, through_chapter)
            if order == "narrative":
                return {"order": order, "snapshot_id": snap["snapshot_id"], "mentions": snap["mentions"]}
            errors = self._validate(snap)
            if errors:
                raise StorageError("；".join(errors))
            groups = layers_for(snap["events"], self._adjacency(snap))
            return {"order": order, "snapshot_id": snap["snapshot_id"], "partial_order": True,
                    "unordered_groups": groups, "events": list(snap["events"].values()),
                    "links": snap["links"], "time_unknown": [e for e in snap["events"] if e not in snap["times"] or snap["times"][e]["start_tick"] is None]}

    def set_time(self, event_id, *, start=None, end=None, frame="main", revealed_at_chapter=None, reason):
        self._reason(reason)
        self._reveal(revealed_at_chapter)
        if any(value is not None and (type(value) not in (int, float) or not math.isfinite(value)) for value in (start, end)):
            raise StorageError("故事时间刻度必须为有限数值。")
        if end is None:
            end = start
        if start is not None and end is not None and start > end:
            raise StorageError("事件结束时间不能早于开始时间。")
        if not frame.strip():
            raise StorageError("时间参照系不能为空。")
        with self.repo.db.transaction() as conn:
            project = self._project_for_event(conn, event_id)
            snap = self._snapshot(conn, project)
            root = event_root(conn, event_id)
            if root not in snap["events"]:
                raise StorageError("只能标注当前已发布事件。")
            old = conn.execute("SELECT * FROM event_times WHERE event_id=?", (root,)).fetchone()
            if start is None and end is None:
                conn.execute("DELETE FROM event_times WHERE event_id=?", (root,))
            else:
                conn.execute("INSERT INTO event_times VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET start_tick=excluded.start_tick,end_tick=excluded.end_tick,frame=excluded.frame,revealed_at_chapter=excluded.revealed_at_chapter", (root, start, end, frame.strip(), revealed_at_chapter))
            errors = self._validate(self._snapshot(conn, project))
            if errors:
                raise StorageError("；".join(errors))
            stored = conn.execute("SELECT * FROM event_times WHERE event_id=?", (root,)).fetchone()
            after = dict(stored) if stored else {"event_id": root, "cleared": True}
            self.repo._audit(conn, project, "event_time", root, dict(old) if old else {}, after, reason)
            self._touch(conn, project)
            return after

    def link(self, source, target, relation, *, reason, status="confirmed", revealed_at_chapter=None):
        self._reason(reason)
        self._reveal(revealed_at_chapter)
        if relation not in RELATIONS or status not in ("confirmed", "unconfirmed", "rejected"):
            raise StorageError("连接类型或状态无效。")
        with self.repo.db.transaction() as conn:
            project = self._project_for_event(conn, source)
            if self._project_for_event(conn, target) != project:
                raise StorageError("不能连接不同项目的事件。")
            snap = self._snapshot(conn, project)
            source, target = event_root(conn, source), event_root(conn, target)
            if source == target or source not in snap["events"] or target not in snap["events"]:
                raise StorageError("连接需要两个不同的当前已发布事件。")
            if relation == "overlaps":
                source, target = sorted((source, target))
            row = conn.execute("SELECT * FROM event_links WHERE from_event_id=? AND to_event_id=? AND relation_type=? AND generation_id=?", (source, target, relation, snap["events"][source]["generation_id"])).fetchone()
            link_id = row["id"] if row else uid()
            before = dict(row) if row else {}
            if row:
                conn.execute("UPDATE event_links SET review_status=? WHERE id=?", (status, link_id))
            else:
                conn.execute("INSERT INTO event_links VALUES(?,?,?,?,?,?)", (link_id, snap["events"][source]["generation_id"], source, target, relation, status))
            conn.execute("INSERT INTO link_visibility VALUES(?,?) ON CONFLICT(link_id) DO UPDATE SET revealed_at_chapter=excluded.revealed_at_chapter", (link_id, revealed_at_chapter))
            return self._finish_link(conn, project, link_id, before, reason)

    def _finish_link(self, conn, project, link_id, before, reason):
        errors = self._validate(self._snapshot(conn, project))
        if errors:
            conn.execute("UPDATE event_links SET review_status='needs_review' WHERE id=?", (link_id,))
        after = dict(self.repo.require(conn, "event_links", link_id))
        self.repo._audit(conn, project, "link", link_id, before, after, reason)
        self._touch(conn, project)
        return {"record": after, "validation_errors": errors}

    def review_link(self, link_id, status, reason):
        self._reason(reason)
        if status not in ("confirmed", "unconfirmed", "rejected"):
            raise StorageError("连接状态无效。")
        with self.repo.db.transaction() as conn:
            before = dict(self.repo.require(conn, "event_links", link_id))
            project = self._project_for_event(conn, before["from_event_id"])
            snap = self._snapshot(conn, project)
            if event_root(conn, before["from_event_id"]) not in snap["events"] or event_root(conn, before["to_event_id"]) not in snap["events"]:
                raise StorageError("连接端点已过期，不能确认。")
            conn.execute("UPDATE event_links SET review_status=? WHERE id=?", (status, link_id))
            return self._finish_link(conn, project, link_id, before, reason)

    def neighbors(self, project_id, event_id, *, direction="both", relation=None, depth=1, max_nodes=100, through_chapter=None):
        if direction not in ("in", "out", "both") or relation not in RELATIONS | {None} or not 1 <= depth <= 20 or not 1 <= max_nodes <= 500:
            raise StorageError("邻接查询参数无效。")
        with self.repo.db.connect() as conn:
            snap = self._snapshot(conn, project_id, through_chapter)
            root = event_root(conn, event_id, through_chapter)
            if root not in snap["events"]:
                raise StorageError("事件不在当前可见范围。")
            pending, seen, found, traversed = deque([(root, 0)]), {root}, [], {}
            truncated = False
            links = [l for l in snap["links"] if l["review_status"] == "confirmed" and (relation is None or l["relation_type"] == relation)]
            while pending:
                node, distance = pending.popleft()
                candidates = [(l, l["target"]) for l in links if direction in ("out", "both") and l["source"] == node]
                candidates += [(l, l["source"]) for l in links if direction in ("in", "both") and l["target"] == node]
                for link, target in candidates:
                    if target not in seen:
                        if distance >= depth or len(seen) >= max_nodes:
                            truncated = True
                            continue
                        seen.add(target)
                        pending.append((target, distance + 1))
                        found.append({"event": snap["events"][target], "distance": distance + 1})
                    if target in seen:
                        traversed[link["id"]] = link
            return {"root": root, "events": found, "links": list(traversed.values()), "truncated": truncated, "snapshot_id": snap["snapshot_id"]}

    def merge_events(self, source, target, *, reason, revealed_at_chapter=None):
        self._reason(reason)
        self._reveal(revealed_at_chapter)
        with self.repo.db.transaction() as conn:
            project = self._project_for_event(conn, source)
            if self._project_for_event(conn, target) != project:
                raise StorageError("不能对齐不同项目的事件。")
            snap = self._snapshot(conn, project)
            source, target = event_root(conn, source), event_root(conn, target)
            if source == target or source not in snap["events"] or target not in snap["events"]:
                raise StorageError("请指定两个不同的当前事件。")
            left, right = snap["times"].get(source), snap["times"].get(target)
            if left and right and any(left[k] != right[k] for k in ("start_tick", "end_tick", "frame")):
                raise StorageError("两个事件的时间标注不同，请先核对或清除时间。")
            conn.execute("INSERT INTO event_identity VALUES(?,?,?)", (source, target, revealed_at_chapter))
            if left and not right:
                copied_reveal = max(left["revealed_at_chapter"], revealed_at_chapter) if left["revealed_at_chapter"] is not None and revealed_at_chapter is not None else None
                conn.execute("INSERT INTO event_times VALUES(?,?,?,?,?)", (target, left["start_tick"], left["end_tick"], left["frame"], copied_reveal))
            errors = self._validate(self._snapshot(conn, project))
            if errors:
                raise StorageError("对齐后时间约束冲突：" + "；".join(errors))
            for plot in conn.execute("SELECT * FROM plot_threads WHERE project_id=?", (project,)).fetchall():
                rows = conn.execute("SELECT * FROM event_thread_roles WHERE plot_thread_id=?", (plot["id"],)).fetchall()
                same = [r for r in rows if event_root(conn, r["event_id"]) == target]
                if same:
                    role = "core" if any(r["role"] == "core" for r in same) else "ordinary"
                    conn.execute("INSERT INTO event_thread_roles VALUES(?,?,?) ON CONFLICT(plot_thread_id,event_id) DO UPDATE SET role=excluded.role", (plot["id"], target, role))
            self.repo._audit(conn, project, "event_identity", source, {"event_id": source}, {"target_id": target, "revealed_at_chapter": revealed_at_chapter}, reason)
            self._touch(conn, project)
            return {"source_id": source, "target_id": target, "evidence_preserved": True, "role_resolution": "core_if_either_core"}

    def create_plot(self, project_id, name, description=""):
        if not name.strip() or len(name) > 200:
            raise StorageError("剧情线名称应为 1～200 字符。")
        with self.repo.db.transaction() as conn:
            self.repo.require(conn, "projects", project_id)
            conn.execute("INSERT OR IGNORE INTO plot_threads VALUES(?,?,?,?)", (uid(), project_id, name.strip(), description))
            self._touch(conn, project_id)
            return dict(conn.execute("SELECT * FROM plot_threads WHERE project_id=? AND name=?", (project_id, name.strip())).fetchone())

    def plots(self, project_id):
        with self.repo.db.connect() as conn:
            self.repo.require(conn, "projects", project_id)
            return [dict(r) for r in conn.execute("SELECT * FROM plot_threads WHERE project_id=? ORDER BY name,id", (project_id,))]

    def set_role(self, plot_id, event_id, role, *, reason):
        self._reason(reason)
        if role not in ("core", "ordinary"):
            raise StorageError("节点角色应为 core 或 ordinary。")
        with self.repo.db.transaction() as conn:
            plot = self.repo.require(conn, "plot_threads", plot_id)
            if self._project_for_event(conn, event_id) != plot["project_id"]:
                raise StorageError("事件与剧情线不属于同一项目。")
            event_id = event_root(conn, event_id)
            if event_id not in self._snapshot(conn, plot["project_id"])["events"]:
                raise StorageError("事件已过期或被否决。")
            old = conn.execute("SELECT * FROM event_thread_roles WHERE plot_thread_id=? AND event_id=?", (plot_id, event_id)).fetchone()
            conn.execute("INSERT INTO event_thread_roles VALUES(?,?,?) ON CONFLICT(plot_thread_id,event_id) DO UPDATE SET role=excluded.role", (plot_id, event_id, role))
            after = {"plot_thread_id": plot_id, "event_id": event_id, "role": role}
            self.repo._audit(conn, plot["project_id"], "event_role", event_id, dict(old) if old else {}, after, reason)
            self._touch(conn, plot["project_id"])
            return after

    @staticmethod
    def _roles(conn, plot_id, snapshot):
        grouped = defaultdict(list)
        for row in conn.execute("SELECT * FROM event_thread_roles WHERE plot_thread_id=?", (plot_id,)):
            root = event_root(conn, row["event_id"], snapshot["through_chapter"])
            if root in snapshot["events"]:
                grouped[root].append(dict(row))
        return {root: next((r["role"] for r in rows if r["event_id"] == root), "core" if any(r["role"] == "core" for r in rows) else "ordinary") for root, rows in grouped.items()}

    def plot_view(self, plot_id, *, expanded=False, max_paths=100, max_depth=30):
        if not 1 <= max_paths <= 500 or not 1 <= max_depth <= 100:
            raise StorageError("路径预算无效。")
        with self.repo.db.connect() as conn:
            plot = self.repo.require(conn, "plot_threads", plot_id)
            snap = self._snapshot(conn, plot["project_id"])
            roles = self._roles(conn, plot_id, snap)
            selected = set(roles) if expanded else {e for e, role in roles.items() if role == "core"}
            nodes = [{**snap["events"][e], "role": roles[e]} for e in sorted(selected)]
            adjacency = defaultdict(list)
            for link in snap["links"]:
                if link["review_status"] == "confirmed" and link["source"] in roles and link["target"] in roles:
                    adjacency[link["source"]].append(link)
            paths, truncated, explored = [], False, 0
            if expanded:
                links = [l for l in snap["links"] if l["review_status"] == "confirmed" and l["source"] in roles and l["target"] in roles]
                return {"plot": dict(plot), "expanded": True, "events": nodes, "links": links, "snapshot_id": snap["snapshot_id"]}
            for start in sorted(selected):
                pending = [(start, [start], [])]
                while pending:
                    explored += 1
                    if explored > max_paths * max_depth:
                        truncated = True
                        pending.clear()
                        break
                    node, visited, edges = pending.pop()
                    for link in adjacency[node]:
                        target = link["target"]
                        if target in visited:
                            continue
                        if len(edges) >= max_depth or len(paths) >= max_paths:
                            truncated = True
                            continue
                        next_nodes, next_edges = visited + [target], edges + [link]
                        if target in selected:
                            paths.append({"source": start, "target": target, "via": next_nodes[1:-1],
                                          "link_ids": [e["id"] for e in next_edges],
                                          "relation_types": [e["relation_type"] for e in next_edges],
                                          "kind": "path_summary"})
                        else:
                            if len(pending) >= min(max_paths * max_depth, 10000):
                                truncated = True
                            else:
                                pending.append((target, next_nodes, next_edges))
            return {"plot": dict(plot), "expanded": False, "events": nodes, "paths": paths, "truncated": truncated, "snapshot_id": snap["snapshot_id"]}
