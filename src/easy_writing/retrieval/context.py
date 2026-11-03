import json

from easy_writing.storage.database import StorageError


def estimate_tokens(value):
    """UTF-8 字节数的保守文本预算估计；不是 DeepSeek 精确 tokenizer。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(text.encode("utf-8"))


class ContextBuilder:
    def __init__(self, budget=4000):
        if not 256 <= budget <= 64000:
            raise StorageError("资料预算应在 256～64000 之间。")
        self.budget = budget

    def build(self, corpus, question, hits, *, sources=(), paths=()):
        coverage = corpus.coverage
        packet = {
            "snapshot": corpus.snapshot_id, "question": question,
            "scope": {"chapter": corpus.through_chapter, "actor_restricted": corpus.actor is not None, "at_event": corpus.at_event},
            "coverage": {"loaded_count": len(coverage["loaded_chapters"]), "not_current": coverage["not_current"][:20],
                         "pending_review_count": coverage.get("pending_review_count", 0), "quarantined_chapters": coverage.get("quarantined_chapters", [])[:20],
                         "missing_ranges": coverage["missing_chapter_ranges"][:10], "full_book_claim": False},
            "records": [], "sources": [], "paths": [], "truncated": False, "retrieval_partial": True,
        }
        if estimate_tokens(packet) > self.budget:
            raise StorageError("预算不足以容纳问题与范围信息，请缩短问题或提高预算。")
        dropped = 0
        for hit in hits:
            record = hit["record"]
            start, end = hit.get("span") or (0, len(record.text))
            end = min(end, start + 800)
            refs = [{k: e[k] for k in ("evidence_id", "version_id", "chapter_no", "paragraph_id")}
                    | ({} if corpus.actor is not None else {"quote": e["quote"]}) for e in record.evidence[:2]]
            item = {"key": record.key, "text": record.text[start:end], "text_window": [start, end, len(record.text)],
                    "status": record.review_status, "uncertainty": record.uncertainty, "evidence": refs,
                    "provenance": {k: v for k, v in record.extra.items() if k in ("mode", "certainty", "author_reviewed", "candidate")}}
            packet["records"].append(item)
            if estimate_tokens(packet) > self.budget:
                item["evidence"] = [{k: v for k, v in e.items() if k != "quote"} for e in item["evidence"]]
                item["evidence_quote_omitted"] = True
            while estimate_tokens(packet) > self.budget and len(item["text"]) > 32:
                item["text"] = item["text"][:len(item["text"])//2]
                item["text_window"][1] = start + len(item["text"])
            if estimate_tokens(packet) > self.budget:
                packet["records"].pop()
                dropped += 1
        for field, items in (("paths", paths), ("sources", sources)):
            for item in items:
                packet[field].append(item)
                if estimate_tokens(packet) > self.budget:
                    packet[field].pop()
                    dropped += 1
        packet["truncated"] = bool(dropped) or any(r["text_window"][1] - r["text_window"][0] < r["text_window"][2] for r in packet["records"])
        # true 比 false 更短；元数据本身不再改变大小上界。
        return {"packet": packet, "estimated_tokens": estimate_tokens(packet), "budget": self.budget,
                "estimator": "utf8-byte-upper-estimate-v1", "dropped_items": dropped}
