"""逐轮提炼和滚动归并。记忆用于创作意图与指代，不能充当小说证据。"""

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from easy_writing.storage.repository import digest


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def scope_key(scope, snapshot, request=None):
    parts = [scope, snapshot]
    if request and request.get("project_guidance"):
        parts.extend((request.get("goal") or "", request.get("constraints") or ""))
    return digest(encode(parts))


def reference_only(result):
    """保留答复和出处定位，移除检索时复制进资料包的正文与知识摘录。"""
    if result is None:
        return None

    def packet_refs(packet):
        return packet | {
            "records": [{k: v for k, v in item.items() if k != "text"} | {
                "evidence": [{k: v for k, v in ref.items() if k != "quote"} for ref in item.get("evidence", [])]
            } for item in packet.get("records", [])],
            "sources": [{k: v for k, v in item.items() if k != "text"} for item in packet.get("sources", [])],
        }

    saved = dict(result)
    if "context" in saved:
        saved["context"] = saved["context"] | {"packet": packet_refs(saved["context"]["packet"])}
    if "supplemental_context" in saved:
        saved["supplemental_context"] = [packet_refs(p) for p in saved["supplemental_context"]]
    return saved


class SemanticSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposals: list[str] = Field(max_length=3)
    unresolved: list[str] = Field(max_length=3)


def turn_summary(turn, question, result):
    memory = result.get("memory", {})
    answer = result["answer"]["answer"]
    sentences = re.split(r"(?<=[。！？；])|\n", answer)
    proposals = [s.strip()[:140] for s in sentences if re.search("建议|可以|方案|铺垫|写法|路线", s)][:3] if memory.get("advice") else []
    return {
        "turn": turn, "focus_id": memory.get("focus_id"), "focus": memory.get("focus_label", ""),
        "question": question[:150], "goal": memory.get("goal", ""), "constraints": memory.get("constraints", ""),
        "outcome": next((sentence.strip()[:140] for sentence in sentences if sentence.strip()), ""),
        "citation_refs": result["answer"].get("citations", [])[:6],
        "author_directives": [d for d in memory.get("directives", []) if d == question[:150]],
        "proposals": proposals, "unresolved": [memory["open_question"][:150]] if memory.get("open_question") else [],
    }


def save_turn_memory(conn, seq, library, session, turn, request, result):
    if not {"scope", "snapshot", "answer"} <= result.keys():
        return
    key = scope_key(result["scope"], result["snapshot"], request)
    summary = turn_summary(turn, request["question"], result)
    # 显式对象优先，创作目标作为讨论主题。未分类的问答也保留逐轮来源。
    topic = summary["focus_id"] or ("goal:" + summary["goal"] if summary["goal"] else "general")
    status = "pending" if request.get("semantic_summary") and result.get("metrics", {}).get("llm_turns") else "local"
    if conn.execute("INSERT OR IGNORE INTO turn_memory VALUES(?,?,?,?,NULL,?)",
                    (seq, key, topic, encode(summary), status)).rowcount:
        refresh_rolling(conn, library, session, key, topic, seq)


def refresh_rolling(conn, library, session, key, topic, changed_seq):
    rows = conn.execute("""SELECT m.turn_seq,m.local_json,m.semantic_json FROM turn_memory m JOIN turns t ON t.seq=m.turn_seq
        WHERE t.library_id=? AND t.session_id=? AND m.scope_key=? AND m.topic=?
        ORDER BY m.turn_seq DESC LIMIT 8""", (library, session, key, topic)).fetchall()
    entries = [json.loads(row["local_json"]) for row in rows]
    for entry, row in zip(entries, rows):
        if row["semantic_json"]:
            entry.update(json.loads(row["semantic_json"]))
    latest = entries[0]
    if changed_seq not in {row["turn_seq"] for row in rows}:
        changed = conn.execute("SELECT local_json,semantic_json FROM turn_memory WHERE turn_seq=?", (changed_seq,)).fetchone()
        entries.append(json.loads(changed["local_json"]) | json.loads(changed["semantic_json"] or "{}"))
    previous = conn.execute("""SELECT summary_json FROM rolling_memory WHERE library_id=? AND session_id=? AND scope_key=? AND topic=?""",
                            (library, session, key, topic)).fetchone()
    previous = json.loads(previous[0]) if previous else {}
    refreshed_turns = {entry["turn"] for entry in entries}
    combined = {field: latest[field] for field in ("turn", "focus", "goal", "constraints")}
    for field in ("proposals", "unresolved", "author_directives"):
        seen, values = set(), []
        # 旧约束可在原轮次追溯，滚动摘要只提供最新的作者指令。
        candidates = entries[:1] if field == "author_directives" else entries
        for entry in candidates:
            for text in entry[field]:
                if text and text not in seen:
                    seen.add(text)
                    values.append({"text": text, "turn": entry["turn"]})
        if field != "author_directives":
            for old in previous.get(field, []):
                if old["turn"] not in refreshed_turns and old["text"] not in seen:
                    seen.add(old["text"])
                    values.append(old)
        combined[field] = values[:4]
    combined["topics"] = list(dict.fromkeys(entry["question"] for entry in entries))[:3]
    conn.execute("""INSERT INTO rolling_memory VALUES(?,?,?,?,?,?)
        ON CONFLICT(library_id,session_id,scope_key,topic)
        DO UPDATE SET summary_json=excluded.summary_json,through_seq=excluded.through_seq""",
        (library, session, key, topic, rows[0]["turn_seq"], encode(combined)))
