"""第四阶段检索验收；默认使用流程测试向量，--embedding bge 使用真实本地中文模型。"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from easy_writing.retrieval.corpus import CorpusBuilder
from easy_writing.retrieval.embeddings import BGEEmbeddingProvider, FakeEmbeddingProvider
from easy_writing.retrieval.hybrid import HybridRetriever
from easy_writing.retrieval.index import VectorIndex
from easy_writing.retrieval.workflow import ResearchWorkflow
from easy_writing.storage.database import Database
from easy_writing.storage.repository import Repository


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stage4_demo/novel.db"))
    parser.add_argument("--embedding", choices=["fake", "bge"], default="fake")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run([sys.executable, str(root / "scripts/demo_stage3.py"), "--db", str(args.db.resolve())],
                             capture_output=True, text=True, encoding="utf-8", check=True)
    baseline = json.loads(process.stdout)
    repo = Repository(Database(args.db))
    project = baseline["project_id"]
    events = {e["title"]: e["id"] for e in repo.records(project, "event")}
    facts = repo.records(project, "fact")
    provider = BGEEmbeddingProvider(cache_folder=root / ".cache/models") if args.embedding == "bge" else FakeEmbeddingProvider()
    cases = [
        ("交接完成以后，开门用的物件归谁保管？", next(f["id"] for f in facts if f["predicate"] == "持有人" and f["chapter_no"] == 5), {"at_event": events["交接钥匙"]}),
        ("兄妹俩是什么亲属关系？", next(f["id"] for f in facts if f["predicate"] == "妹妹"), {}),
        ("地下房间的通行口令是什么？", next(f["id"] for f in facts if f["predicate"] == "密码"), {}),
    ]
    evaluation, routine_reads = [], []
    for question, expected, scope in cases:
        corpus = CorpusBuilder(repo).build(project, **scope)
        result = HybridRetriever(repo, provider).search(corpus, question, limit=5)
        rank = next((i for i, hit in enumerate(result["hits"], 1) if hit["record"].id == expected), None)
        if args.embedding == "bge":
            assert rank is not None, "真实语义换措辞召回未通过"
        evaluation.append({"query": question, "expected_fact_id": expected, "rank_in_top5": rank})
        report = ResearchWorkflow(repo, provider, source_mode="auto").run(project, question, **scope)
        routine_reads.append(report["metrics"]["source_read_calls"])
    corpus = CorpusBuilder(repo).build(project)
    assert VectorIndex(repo, provider).ensure(corpus)["new_segments"] == 0
    reader = ResearchWorkflow(repo, provider).run(project, "第十章地下房间的通行口令是什么？")
    assert "青石" not in json.dumps(reader, ensure_ascii=False)
    verification = ResearchWorkflow(repo, provider).run(project, "请核对原文，钥匙交接给了谁？", subject="钥匙", predicate="持有人", at_event=events["交接钥匙"])
    assert verification["context"]["packet"]["sources"]
    assert all(s["unit"] in ("quote", "local_paragraph") for s in verification["context"]["packet"]["sources"])
    ordinary = HybridRetriever(repo, provider).search(corpus, "回忆修复", limit=8)
    assert any(h["record"].id == events["回忆修复"] for h in ordinary["hits"])
    long_text = "钥匙归属和人物关系需要依据原文。"*80 + "末尾的银色锁片也必须保留。"
    parts = provider.split(long_text)
    assert "".join(p.text for p in parts) == long_text
    assert all(p.token_count <= provider.max_tokens for p in parts)
    provider.checked_encode([p.text for p in parts])
    result = {"project_id": project, "database": str(args.db.resolve()), "embedding": provider.metadata,
              "semantic_evaluation": evaluation if args.embedding == "bge" else "fake 仅验证流程，不评估语义质量",
              "routine_source_reads": routine_reads, "reader_boundary_verified": True,
              "ordinary_node_recalled": True, "repeat_index_new_segments": 0,
              "verification_source_fragments": verification["metrics"]["source_fragments"],
              "verification_context_tokens_estimate": verification["context"]["estimated_tokens"],
              "long_text_segments": len(parts), "long_text_preserved": True,
              "real_model_calls": 0, "embedding_load_ms": getattr(provider, "load_ms", 0)}
    args.db.parent.mkdir(parents=True, exist_ok=True)
    (args.db.parent / "stage4_acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
