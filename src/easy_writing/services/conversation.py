"""本地会话、范围受限的记忆和逐轮创作问答。"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from easy_writing.config import ConfigurationError
from easy_writing.llm.base import LLMError
from easy_writing.services.chat_memory import SemanticSummary, encode, reference_only, refresh_rolling, save_turn_memory, scope_key
from easy_writing.services.review_scope import ReviewScope, text_value
from easy_writing.storage.chat_database import ChatDatabase, migrate_legacy
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import digest, now, uid


NORMAL_BUDGET = (3, 3, 2)
DEEP_BUDGET = (5, 8, 4)
KNOWLEDGE_QUESTION = re.compile("知道|知晓|获知|知情")
POINT_REFERENCE = re.compile("当时|那时|彼时")
EVENT_REFERENCE = re.compile("这件事|该事件|此事|那个事件")
FOLLOW_UP = re.compile("他|她|其|这个人|这件事|该事件|当时|后来|之前|继续|接着|该人物|此事|那个事件")
ADVICE = re.compile("建议|写法|怎么写|如何写|下一章|铺垫|揭晓|续写|改写|写作|推进")
DIRECTIVE = re.compile("保留|不要|必须|改成|推迟|延后|提前|只允许|取消")
GOAL = re.compile(r"(?:创作目标[：:]|目标是|我想让)([^。！？\n]{1,120})")
GUIDANCE_LIMIT = 1200


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=750)
    through_chapter: int | None = Field(default=None, ge=1)
    actor: str | None = None
    at_event: str | None = None
    focus: str | None = None
    goal: str | None = Field(default=None, max_length=GUIDANCE_LIMIT)
    constraints: str | None = Field(default=None, max_length=GUIDANCE_LIMIT)
    project_guidance: bool = False
    deep: bool = False
    allow_pending: bool = False
    provider: str = "fake"
    embedding: str = "none"
    semantic_summary: bool = False


class ConversationService:
    def __init__(self, repo, chat_path=None):
        self.repo = repo
        path = Path(chat_path) if chat_path is not None else repo.db.path.with_name("easy_writing_chat.db")
        if path.resolve() == repo.db.path.resolve():
            raise StorageError("会话库和小说知识库需要使用两个文件。")
        self.db = ChatDatabase(path)
        with repo.db.connect() as conn:
            self.library = conn.execute("SELECT id FROM knowledge_identity WHERE singleton=1").fetchone()[0]
        migrate_legacy(repo, self.db, self.library)

    def guidance(self, project):
        with self.db.connect() as conn:
            row = conn.execute("SELECT goal,constraints_text FROM project_guidance WHERE library_id=? AND project_id=?",
                               (self.library, project)).fetchone()
        return tuple(row) if row else ("", "")

    def save_guidance(self, project, goal, constraints):
        if any(not isinstance(value, str) or len(value) > GUIDANCE_LIMIT for value in (goal, constraints)):
            raise StorageError(f"目标与约束各最多 {GUIDANCE_LIMIT} 字。")
        with self.repo.db.connect() as conn:
            self.repo.require(conn, "projects", project)
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO project_guidance VALUES(?,?,?,?)
                ON CONFLICT(library_id,project_id) DO UPDATE SET goal=excluded.goal,constraints_text=excluded.constraints_text""",
                         (self.library, project, goal, constraints))

    def create(self, project, title):
        title = text_value(title, "会话名称", 100)
        session = uid()
        with self.repo.db.connect() as conn:
            self.repo.require(conn, "projects", project)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (self.library, session, project, title, now()))
        return session

    def sessions(self, project):
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM sessions WHERE library_id=? AND project_id=? ORDER BY created_at DESC,rowid DESC", (self.library, project))]

    def rename(self, project, session, title):
        title = text_value(title, "会话名称", 100)
        with self.db.transaction() as conn:
            self._require(conn, project, session)
            conn.execute("UPDATE sessions SET title=? WHERE library_id=? AND project_id=? AND id=?",
                         (title, self.library, project, session))

    def delete(self, project, session):
        with self.db.transaction() as conn:
            self._require(conn, project, session)
            turns = "SELECT seq FROM turns WHERE library_id=? AND session_id=?"
            keys = (self.library, session)
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            if conn.execute(f"SELECT 1 FROM turns WHERE seq IN ({turns}) AND status='running' AND created_at>=?",
                            (*keys, cutoff)).fetchone():
                raise StorageError("该会话有进行中的问答，请等待任务结束。")
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            if conn.execute(f"SELECT 1 FROM summary_attempts WHERE turn_seq IN ({turns}) AND status='running' AND created_at>=?",
                            (*keys, cutoff)).fetchone():
                raise StorageError("该会话有进行中的摘要，请等待任务结束。")
            conn.execute(f"DELETE FROM summary_attempts WHERE turn_seq IN ({turns})", keys)
            conn.execute(f"DELETE FROM turn_memory WHERE turn_seq IN ({turns})", keys)
            conn.execute("DELETE FROM rolling_memory WHERE library_id=? AND session_id=?", keys)
            conn.execute("DELETE FROM turns WHERE library_id=? AND session_id=?", keys)
            conn.execute("DELETE FROM sessions WHERE library_id=? AND project_id=? AND id=?", (self.library, project, session))

    def _require(self, conn, project, session):
        if not conn.execute("SELECT 1 FROM sessions WHERE library_id=? AND project_id=? AND id=?", (self.library, project, session)).fetchone():
            raise StorageError("会话不属于本项目。")

    def turns(self, project, session, *, limit=30, offset=0, before=None, completed=False):
        with self.db.connect() as conn:
            self._require(conn, project, session)
            rows = conn.execute("""SELECT t.*,m.summary_status,m.local_json,m.semantic_json,
                (SELECT a.usage_json FROM summary_attempts a WHERE a.turn_seq=t.seq ORDER BY a.created_at DESC,a.id DESC LIMIT 1) AS summary_usage
                FROM turns t
                LEFT JOIN turn_memory m ON m.turn_seq=t.seq WHERE t.library_id=? AND t.session_id=?
                AND (NOT ? OR t.status='succeeded')
                AND (? IS NULL OR t.seq<?) ORDER BY t.seq DESC LIMIT ? OFFSET ?""",
                (self.library, session, completed, before, before, limit, offset)).fetchall()
        return [{"seq": row["seq"], "id": row["id"], "status": row["status"], "error": row["error_code"],
                 "request": json.loads(row["request_json"]),
                 "summary_status": row["summary_status"],
                 "summary": (json.loads(row["local_json"]) | json.loads(row["semantic_json"] or "{}")) if row["local_json"] else None,
                 "summary_usage": json.loads(row["summary_usage"]) if row["summary_usage"] else None,
                 "result": json.loads(row["result_json"]) if row["result_json"] else None} for row in reversed(rows)]

    def snapshot(self, project, chapter):
        with self.repo.db.connect() as conn:
            return ReviewScope(self.repo, conn, project, chapter).snap["snapshot_id"]

    def _older_context(self, session, before, key, query):
        from easy_writing.retrieval.hybrid import terms_for

        terms = sorted(terms_for(query))[:16]
        if not terms:
            return []
        # 只扫描旧轮次的问题与同范围快照，命中后再读取简短答复。
        rank = " + ".join("(instr(lower(json_extract(t.request_json,'$.question')),?)>0)" for _ in terms)
        with self.db.connect() as conn:
            return [{"问题": row["question"], "回复": row["answer"]} for row in conn.execute(
                f"""SELECT substr(json_extract(t.request_json,'$.question'),1,90) AS question,
                substr(json_extract(t.result_json,'$.answer.answer'),1,110) AS answer, ({rank}) AS score
                FROM turns t JOIN turn_memory m ON m.turn_seq=t.seq
                WHERE t.library_id=? AND t.session_id=? AND t.seq<? AND m.scope_key=?
                AND score>0 ORDER BY score DESC,t.seq DESC LIMIT 2""",
                (*terms, self.library, session, before, key))]

    def _rolling(self, session, before, key, topic, query):
        from easy_writing.retrieval.hybrid import terms_for

        terms = sorted(terms_for(query))[:16]
        score = " + ".join("(instr(lower(summary_json),?)>0)" for _ in terms) or "0"
        with self.db.connect() as conn:
            rows = conn.execute(f"""SELECT summary_json, (topic=?) AS exact, ({score}) AS rank
                FROM rolling_memory WHERE library_id=? AND session_id=? AND scope_key=? AND through_seq<?
                AND (exact OR rank>0) ORDER BY exact DESC,rank DESC,topic LIMIT 2""",
                (topic, *terms, self.library, session, key, before))
            return [json.loads(row["summary_json"]) for row in rows]

    def _prepare(self, project, session, sequence, request, previous):
        from easy_writing.services.knowledge import KnowledgeService

        latest_turn = next((turn for turn in reversed(previous) if turn["result"]), {})
        latest = latest_turn.get("result", {})
        latest_memory = latest.get("memory", {})
        prior_scope = latest.get("scope", {})
        chapter_limit = request.through_chapter if "through_chapter" in request.model_fields_set else prior_scope.get("through_chapter")
        requested_actor = request.actor if "actor" in request.model_fields_set else prior_scope.get("actor")
        requested_point = request.at_event if "at_event" in request.model_fields_set else prior_scope.get("at_event")
        embedded = KnowledgeService._chapter_number(request.question)
        if embedded is not None and chapter_limit is not None and embedded > chapter_limit:
            raise StorageError("问题超出设置的正文范围，请调整范围后重新发送。")
        chapter = embedded if embedded is not None else chapter_limit
        with self.repo.db.connect() as conn:
            review = ReviewScope(self.repo, conn, project, chapter)
            snapshot = review.snap["snapshot_id"]
            actors = {i: e for i, e in review.knowledge._catalog(conn, review.snap, current_only=True).items() if e["kind"] == "person"}
            point = review.event(requested_point) if requested_point else None
            events = {i: e for i, e in review.snap["events"].items()
                      if point is None or review.compare(i, point) in ("equal", "before")}
            actor = review.entity(requested_actor) if requested_actor else None
            if actor and actor not in actors:
                raise StorageError("认知视角必须是当前范围内的人物。")
            labels = {}
            for i, person in actors.items():
                for label in person["aliases"] | {person["name"]}:
                    if label and label in request.question:
                        labels.setdefault(label, []).append(i)
            matches = {i for name, ids in labels.items() if not any(name != other and name in other for other in labels) for i in ids}
            matched_events = {i for i, event in events.items() if event["title"] and event["title"] in request.question}
            anchor = next(iter(matched_events)) if len(matched_events) == 1 else latest_memory.get("anchor_event")
            if anchor not in events:
                anchor = None
            focus = request.focus
            if focus and focus not in actors and focus not in events:
                raise StorageError("所选对象超出当前章节或故事时点范围。")
            ambiguity = ""
            if focus is None:
                if any(len(ids) > 1 for label, ids in labels.items() if set(ids) <= matches):
                    ambiguity = "这个名字对应多个人物。请通过「明确查询对象」选择要查询的人物。"
                elif len(matched_events) > 1 and len({events[i]["title"] for i in matched_events}) < len(matched_events):
                    ambiguity = "有多个同名事件。请选择要查询的事件或补充章节。"
                elif len(matches) == 1:
                    focus = next(iter(matches))
                elif len(matched_events) == 1:
                    focus = next(iter(matched_events))
                elif not matches and FOLLOW_UP.search(request.question):
                    old_focus = anchor if anchor and EVENT_REFERENCE.search(request.question) else latest_memory.get("focus_id")
                    if old_focus in actors or old_focus in events:
                        focus = old_focus
            if focus in events and (request.focus or matched_events):
                anchor = focus
            if not actor and focus == prior_scope.get("actor") and focus in actors and not matches and FOLLOW_UP.search(request.question):
                actor = focus
            asks_knowledge = KNOWLEDGE_QUESTION.search(request.question) and not ADVICE.search(request.question)
            if not actor and asks_knowledge:
                if focus in actors:
                    actor = focus
                elif not matches and FOLLOW_UP.search(request.question) and latest_memory.get("focus_id") in actors:
                    actor = latest_memory["focus_id"]
                elif not ambiguity:
                    ambiguity = "请明确是哪位人物的认知，或在表单中选择人物视角。"
            if actor and asks_knowledge and point is None and (len(matched_events) == 1 or POINT_REFERENCE.search(request.question)):
                point = anchor
                if point is None and embedded is None and chapter is None and not ambiguity:
                    ambiguity = "请补充「当时」所指的事件、章节，或选择故事时点。"
            if focus is None and not ambiguity and re.match(r"他|她|这个人|这件事|该人物|该事件|此事", request.question):
                ambiguity = "请补充所指的人物或事件，或通过「明确查询对象」选择。"
            if actor and focus not in (None, actor):
                focus = actor  # 角色范围只使用自身名称与已获知的命题，不带入事件摘要。
            focus_kind = "person" if focus in actors else "event" if focus in events else None
            focus_label = actors[focus]["name"] if focus_kind == "person" else events[focus]["title"] if focus_kind == "event" else ""

        scope = {"through_chapter": chapter, "actor": actor, "at_event": point}
        key = scope_key(scope, snapshot, request.model_dump())
        same = [turn for turn in previous if turn["result"] and turn["result"].get("scope") == scope
                and turn["result"].get("snapshot") == snapshot
                and scope_key(scope, snapshot, turn["request"]) == key]
        prior = same[-1]["result"].get("memory", {}) if same else {}
        same_scope = latest.get("scope") == scope
        # 作者明确提供的目标在同一范围内可跨改稿保留，小说依据按快照重新获取。
        goal = request.goal if request.goal is not None else latest_memory.get("goal", "") if same_scope else ""
        constraints = request.constraints if request.constraints is not None else latest_memory.get("constraints", "") if same_scope else ""
        if request.project_guidance:
            goal, constraints = request.goal or "", request.constraints or ""
        elif latest and not same_scope:
            if goal == latest_memory.get("goal"):
                goal = ""
            if constraints == latest_memory.get("constraints"):
                constraints = ""
        stated_goal = GOAL.search(request.question)
        if not request.project_guidance and stated_goal and (not request.goal or request.goal == latest_memory.get("goal")):
            goal = stated_goal[1].strip()
        directives = prior.get("directives", [])
        advice = bool(ADVICE.search(request.question) or stated_goal or (goal and not request.project_guidance) or
                      (prior.get("advice") and (DIRECTIVE.search(request.question) or re.search("换一个|还有|另一种|继续|接着", request.question))))
        if DIRECTIVE.search(request.question) and advice:
            directives = (directives + [request.question[:150]])[-6:]
        remembered = focus
        if remembered is None and not matches and not matched_events and not ambiguity and same_scope:
            previous_focus = latest_memory.get("focus_id")
            if previous_focus in actors or (not actor and previous_focus in events):
                remembered = previous_focus
        recent = same[-3:]
        memory = {"focus_id": remembered, "focus_kind": focus_kind, "focus_label": focus_label, "anchor_event": anchor,
                  "goal": goal, "constraints": constraints, "directives": directives, "advice": advice,
                  "recent": [{"问题": turn["request"]["question"][:130], "回复": turn["result"]["answer"]["answer"][:160]}
                             for turn in recent],
                  "summary": self._older_context(session, recent[0]["seq"] if recent else sequence, key,
                                                 focus_label + " " + request.question),
                  "rolling": self._rolling(session, sequence, key, remembered or ("goal:" + goal if goal else "general"),
                                           focus_label + " " + request.question),
                  "open_question": prior.get("open_question", "")}
        return scope, snapshot, memory, ambiguity

    def send(self, project, session, turn_id, request, *, client_factory, summary_client_factory=None, embedding=None,
             recorder=None, model_first=False):
        from easy_writing.retrieval.workflow import ResearchWorkflow

        text_value(turn_id, "对话轮次编号", 100)
        payload = request.model_dump()
        if not request.project_guidance:
            payload.pop("project_guidance")
        fingerprint = digest(encode([session, payload, sorted(request.model_fields_set & {"through_chapter", "actor", "at_event"})]))
        legacy_payload = {k: v for k, v in payload.items() if k != "semantic_summary"}
        legacy_hash = digest(json.dumps(["research", {"session": session, **legacy_payload}], ensure_ascii=False, sort_keys=True))
        with self.db.transaction() as conn:
            self._require(conn, project, session)
            previous = conn.execute("SELECT * FROM turns WHERE library_id=? AND id=?", (self.library, turn_id)).fetchone()
            if previous:
                valid = (fingerprint,) if request.semantic_summary else (fingerprint, legacy_hash)
                if previous["session_id"] != session or previous["request_hash"] not in valid:
                    raise StorageError("对话轮次编号已绑定其他请求。")
                if previous["status"] == "succeeded":
                    return {"task_id": turn_id, "cached": True, "result": json.loads(previous["result_json"])}
                raise StorageError("该轮已提交。刷新不会重发，请明确提交新问题。")
            sequence = conn.execute("""INSERT INTO turns(library_id,session_id,id,request_json,request_hash,status,created_at)
                VALUES(?,?,?,?,?,'running',?)""", (self.library, session, turn_id, encode(payload), fingerprint, now())).lastrowid

        def execute():
            previous = self.turns(project, session, limit=16, before=sequence, completed=True)
            scope, snapshot, memory, ambiguity = self._prepare(project, session, sequence, request, previous)
            if ambiguity:
                return {"answer": {"answer": ambiguity, "citations": [], "confidence": "insufficient"},
                        "scope": scope, "snapshot": snapshot, "memory": memory | {"open_question": ambiguity},
                        "metrics": {"llm_turns": 0, "stop_reason": "clarify"}}
            calls, tools, reads = DEEP_BUDGET if request.deep else NORMAL_BUDGET
            client = client_factory()
            model_memory = {key: value for key, value in memory.items() if key != "anchor_event"}
            try:
                result = ResearchWorkflow(self.repo, embedding, client=client, recorder=recorder,
                                          max_llm_calls=calls, max_tool_calls=tools, max_source_reads=reads,
                                          deep=request.deep, allow_pending=request.allow_pending, model_first=model_first,
                                          context_budget=4200 if request.deep else 6000,
                                          conversation={"snapshot": snapshot, "advice": memory["advice"],
                                                        "memory": model_memory, **{key: memory[key] for key in ("focus_id", "focus_kind", "focus_label")}}
                                          ).run(project, request.question, **scope)
            finally:
                client.close()
            answer = result["answer"]
            if not result["metrics"]["llm_turns"] and not result["metrics"]["stop_reason"]:
                if result["retrieval"].get("strategy") == "chapter_overview":
                    answer["answer"] = f"第 {scope['through_chapter']} 章目前没有可用的摘要或事件。请检查该章的入库与复核状态。"
                else:
                    answer["answer"] = ("当前可见知识中没有检索到相关依据。可以补充人物、事件或章节信息。"
                                        if not scope["actor"] else "当前范围内没有检索到该人物获知相关信息的证据。")
                result["metrics"]["stop_reason"] = "no_evidence"
            memory["open_question"] = answer["answer"][:150] if answer["confidence"] in ("insufficient", "clarify") else ""
            return result | {"scope": scope, "snapshot": snapshot, "memory": memory}

        try:
            result = reference_only(execute())
        except BaseException as error:
            code = error.code if isinstance(error, LLMError) else "task_failed"
            with self.db.transaction() as conn:
                conn.execute("UPDATE turns SET status='failed',error_code=?,finished_at=? WHERE seq=? AND library_id=? AND id=?",
                             (code, now(), sequence, self.library, turn_id))
            raise
        with self.db.transaction() as conn:
            if not conn.execute("""UPDATE turns SET status='succeeded',result_json=?,finished_at=?
                WHERE seq=? AND library_id=? AND id=?""", (encode(result), now(), sequence, self.library, turn_id)).rowcount:
                raise StorageError("会话轮次已删除。")
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM turns WHERE seq=? AND library_id=? AND id=?",
                                (sequence, self.library, turn_id)).fetchone():
                raise StorageError("会话轮次已删除。")
            save_turn_memory(conn, sequence, self.library, session, turn_id, payload, result)
        if request.semantic_summary and result["metrics"].get("llm_turns"):
            self.summarize(project, session, turn_id, client_factory=summary_client_factory or client_factory)
        return {"task_id": turn_id, "cached": False, "result": result}

    def summarize(self, project, session, turn_id, *, client_factory):
        attempt = uid()
        with self.db.transaction() as conn:
            self._require(conn, project, session)
            turn = conn.execute("""SELECT * FROM turns WHERE library_id=? AND session_id=? AND id=? AND status='succeeded'""",
                                (self.library, session, turn_id)).fetchone()
            if not turn:
                raise StorageError("找不到已完成的对话轮次。")
            request, result = json.loads(turn["request_json"]), json.loads(turn["result_json"])
            seq = turn["seq"]
            save_turn_memory(conn, seq, self.library, session, turn_id, request, result)
            memory = conn.execute("SELECT * FROM turn_memory WHERE turn_seq=?", (seq,)).fetchone()
            if not memory or memory["summary_status"] == "succeeded":
                return
            if memory["summary_status"] == "running":
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
                if conn.execute("SELECT 1 FROM summary_attempts WHERE turn_seq=? AND status='running' AND created_at>=?", (seq, cutoff)).fetchone():
                    raise StorageError("该轮摘要正在生成。")
                conn.execute("UPDATE summary_attempts SET status='failed' WHERE turn_seq=? AND status='running'", (seq,))
            conn.execute("INSERT INTO summary_attempts VALUES(?,?,?,'running',NULL)", (attempt, seq, now()))
            conn.execute("UPDATE turn_memory SET summary_status='running' WHERE turn_seq=?", (seq,))
        data = None
        metrics = {"call_id": None, "usage": None}
        try:
            client = client_factory()
            try:
                response = client.extract_json(
                    encode({"question": request["question"], "answer": result["answer"]["answer"][:900]}),
                    SemanticSummary,
                    system_prompt="提炼本轮讨论过的写作方案与待核查事项。只保留源消息已有的内容，不推断小说事实、作者决定或新约束。"
                                  "proposals 是建议，unresolved 是未解决事项。无内容时输出空列表。")
            finally:
                client.close()
            metrics = {"call_id": response.call_id, "usage": response.usage.model_dump(), "provider": response.provider}
            data = {key: [value.strip()[:150] for value in values if value.strip()]
                    for key, values in response.content.model_dump().items()}
        except (LLMError, ConfigurationError, ValidationError) as error:
            usage = getattr(error, "usage", None)
            metrics = {"call_id": getattr(error, "call_id", None), "usage": usage.model_dump() if usage else None,
                       "status": getattr(error, "code", "summary_failed")}
        with self.db.transaction() as conn:
            status = "succeeded" if data is not None else "failed"
            if conn.execute("""UPDATE summary_attempts SET status=?,usage_json=? WHERE id=? AND status='running'""",
                            (status, encode(metrics), attempt)).rowcount:
                conn.execute("UPDATE turn_memory SET summary_status=?,semantic_json=? WHERE turn_seq=?",
                             (status, encode(data) if data is not None else None, seq))
                refresh_rolling(conn, self.library, session, memory["scope_key"], memory["topic"], seq)
