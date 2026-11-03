import json
from time import perf_counter

import numpy as np

from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import digest


class VectorIndex:
    def __init__(self, repo, provider):
        self.repo, self.provider = repo, provider

    def ensure(self, corpus):
        start = perf_counter()
        provider = self.provider
        missing, cached, tokens = [], 0, 0
        with self.repo.db.connect() as conn:
            for record in corpus.records:
                segments = provider.split(record.text)
                rows = conn.execute("""SELECT segment_no FROM vector_segments WHERE project_id=? AND record_type=?
                    AND record_id=? AND source_hash=? AND model_fingerprint=?""",
                    (corpus.project_id, record.kind, record.id, digest(record.text), provider.fingerprint)).fetchall()
                present = {r[0] for r in rows}
                for number, segment in enumerate(segments):
                    if number in present:
                        cached += 1
                    else:
                        missing.append((record, number, segment))
        vectors = []
        for offset in range(0, len(missing), 64):
            group = missing[offset:offset+64]
            vectors.extend(provider.checked_encode([segment.text for _, _, segment in group]))
            tokens += sum(segment.token_count for _, _, segment in group)
        with self.repo.db.transaction() as conn:
            meta = provider.metadata
            conn.execute("INSERT OR IGNORE INTO embedding_models VALUES(?,?,?,?,?,?)", (
                provider.fingerprint, provider.model_name, provider.revision, provider.dimension,
                provider.max_tokens, json.dumps(meta, ensure_ascii=False, sort_keys=True)))
            for (record, number, segment), vector in zip(missing, vectors):
                conn.execute("INSERT OR REPLACE INTO vector_segments VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    corpus.project_id, record.kind, record.id, record.generation_id, digest(record.text),
                    provider.fingerprint, number, segment.start, segment.end, segment.token_count,
                    np.asarray(vector, dtype="<f4").tobytes()))
        return {"new_segments": len(missing), "cached_segments": cached, "embedding_tokens": tokens,
                "latency_ms": round((perf_counter()-start)*1000, 2), "model": provider.metadata,
                "model_fingerprint": provider.fingerprint}

    def scores(self, corpus, query):
        vector = self.provider.checked_encode([query], query=True)[0]
        scores, self.match_spans = {}, {}
        with self.repo.db.connect() as conn:
            for record in corpus.records:
                rows = conn.execute("""SELECT vector,start_offset,end_offset FROM vector_segments WHERE project_id=? AND record_type=?
                    AND record_id=? AND source_hash=? AND model_fingerprint=?""",
                    (corpus.project_id, record.kind, record.id, digest(record.text), self.provider.fingerprint)).fetchall()
                best = None
                for row in rows:
                    cached = np.frombuffer(row[0], dtype="<f4")
                    if cached.shape != (self.provider.dimension,) or not np.isfinite(cached).all():
                        raise StorageError("向量缓存损坏或维度不匹配，请检查数据库。")
                    norm = float(np.linalg.norm(cached))
                    if norm <= 1e-12:
                        raise StorageError("向量缓存包含无效零向量。")
                    score = float(np.dot(cached / norm, vector))
                    if best is None or score > best:
                        best = score
                        self.match_spans[record.key] = (row["start_offset"], row["end_offset"])
                if best is not None:
                    scores[record.key] = best
        return scores
