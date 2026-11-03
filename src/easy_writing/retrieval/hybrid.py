import re
from time import perf_counter

from easy_writing.retrieval.index import VectorIndex
from easy_writing.storage.database import StorageError


def terms_for(query):
    terms = set(re.findall(r"[A-Za-z0-9_]{2,}", query.lower()))
    for group in re.findall(r"[\u4e00-\u9fff]+", query):
        terms.update(group[i:i+2] for i in range(len(group)-1))
    return terms


class HybridRetriever:
    def __init__(self, repo, provider=None):
        self.repo, self.provider = repo, provider
        self.calls = []

    def search(self, corpus, query, *, limit=10):
        if not query.strip() or not 1 <= limit <= 50:
            raise StorageError("检索问题不能为空，结果数量应在 1～50 之间。")
        start = perf_counter()
        index_report = {"new_segments": 0, "embedding_tokens": 0}
        semantic, spans = {}, {}
        if self.provider is not None:
            if self.provider.count_tokens(query, query=True) > self.provider.max_tokens:
                raise StorageError("查询超过嵌入模型输入上限，请先缩短问题。")
            index = VectorIndex(self.repo, self.provider)
            index_report = index.ensure(corpus)
            semantic = index.scores(corpus, query)
            spans = index.match_spans
        tokens = terms_for(query)
        lexical = {record.key: sum(term in record.text.lower() for term in tokens) for record in corpus.records}
        entity_hits = {e["id"] for e in corpus.catalog.values()
                       if any(label and label in query for label in e["aliases"] | {e["name"]})}
        exact = {record.key: len(entity_hits.intersection(record.entity_ids)) + int(record.id in query) for record in corpus.records}
        ranks, channels = {}, {}
        for name, values, weight in (("exact", exact, 1.5), ("keyword", lexical, 1.0), ("semantic", semantic, 1.0)):
            values = [(key, score) for key, score in values.items() if score > (0.15 if name == "semantic" else 0)]
            values.sort(key=lambda item: (-item[1], item[0]))
            for rank, (key, score) in enumerate(values, 1):
                ranks[key] = ranks.get(key, 0) + weight / (60 + rank)
                channels.setdefault(key, {})[name] = score
        ordered = sorted((r for r in corpus.records if r.key in ranks), key=lambda r: (-ranks[r.key], r.key))
        self.calls.append({"embedding_tokens": index_report["embedding_tokens"] + (self.provider.count_tokens(query, query=True) if self.provider else 0), "new_segments": index_report["new_segments"]})
        return {"hits": [{"record": r, "score": ranks[r.key], "channels": channels[r.key], "span": spans.get(r.key)} for r in ordered[:limit]],
                "matched_records": len(ordered), "partial": True, "index": index_report,
                "query_embedding_tokens": self.provider.count_tokens(query, query=True) if self.provider else 0,
                "latency_ms": round((perf_counter()-start)*1000, 2)}
