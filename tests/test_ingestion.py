import json
import sqlite3

import pytest

from easy_writing.ingestion.chunker import split_chapter
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.models import ChapterExtraction, validate_evidence
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.llm.base import LLMError
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository

TEXT = "林舟把备用钥匙交给妹妹。"


def payload(paragraph_id="p0001", quote=TEXT, *, distinct=False):
    ev = [{"paragraph_id": paragraph_id, "quote": quote}]
    return {
        "summary": "林舟将钥匙交给妹妹", "summary_evidence": ev,
        "entities": [
            {"key": "p1", "name": "林舟", "kind": "person", "distinct": distinct, "evidence": ev},
            {"key": "p2", "name": "妹妹", "kind": "person", "evidence": ev},
            {"key": "key1", "name": "备用钥匙", "kind": "item", "evidence": ev},
        ],
        "events": [{"key": "e1", "title": "交接钥匙", "summary": "林舟把钥匙交给妹妹", "participants": ["p1", "p2"], "evidence": ev}],
        "facts": [{"key": "f1", "subject": "key1", "predicate": "持有人", "object_value": "妹妹", "object_entity": "p2", "effective_from": "e1", "evidence": ev}],
        "foreshadowing": [{"key": "t1", "title": "钥匙用途", "description": "钥匙可能关联后续情节，待作者确认", "evidence": ev}],
    }


@pytest.fixture
def repo(tmp_path):
    return Repository(Database(tmp_path / "novel.db"))


@pytest.fixture
def project(repo):
    return repo.create_project("测试小说")["id"]


def pipeline(repo, recorder, blocks=None, **kwargs):
    client = FixtureLLMClient(recorder, blocks or {"b0001": payload()})
    return IngestionPipeline(repo, client, model_identity="fixture-test-v1", **kwargs)


def ingest(repo, recorder, project, **kwargs):
    return pipeline(repo, recorder).ingest(project, 1, "交接", TEXT, **kwargs)


def count(repo, table):
    with repo.db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_migration_and_foreign_keys(repo):
    Database(repo.db.path)
    with repo.db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Database.VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO entities(id,project_id,name,kind) VALUES('e','missing','x','person')")


def test_future_database_version_rejected(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(StorageError, match="升级"):
        Database(path)


def test_project_creation_idempotent(repo):
    a = repo.create_project("作品")
    b = repo.create_project("作品")
    assert a["id"] == b["id"]


def test_publish_and_evidence_roundtrip(repo, recorder, project):
    result = ingest(repo, recorder, project)
    assert result["status"] == "published"
    assert result["model_calls"] == 1
    assert count(repo, "entities") == 3
    fact = repo.records(project, "fact")[0]
    assert fact["object_value"] == "妹妹"
    assert fact["subject_name"] == "备用钥匙"
    evidence = repo.evidence("fact", fact["id"])[0]
    assert evidence["quote"] == TEXT
    assert TEXT[evidence["absolute_start"]:evidence["absolute_end"]] == evidence["quote"]
    assert evidence["is_latest_source"]
    assert repo.chapters(project)[0]["knowledge_is_current"]
    assert repo.records(project, "thread")[0]["state"] == "candidate"


def test_duplicate_import_does_not_call_model_or_duplicate_knowledge(repo, recorder, project):
    first = ingest(repo, recorder, project)
    second = ingest(repo, recorder, project)
    assert second["cached"] and second["model_calls"] == 0
    assert first["job_id"] == second["job_id"]
    assert len(recorder.events) == 1
    assert count(repo, "facts") == count(repo, "chapter_versions") == 1


def test_five_chapters_share_unique_exact_entities(repo, recorder, project):
    pipe = pipeline(repo, recorder)
    for chapter in range(1, 6):
        pipe.ingest(project, chapter, f"第{chapter}章", TEXT)
    assert len(repo.chapters(project)) == len(repo.records(project, "fact")) == 5
    assert len(repo.records(project, "entity")) == 3
    assert len(recorder.events) == 5
    assert len(repo.records(project, "event", limit=2, offset=2)) == 2


def test_original_newlines_and_offsets_preserved():
    text = "\r\n  林舟来了。\r\n\r\n妹妹走了。\n"
    paragraphs, blocks = split_chapter(text)
    for p in paragraphs:
        assert text[p.start:p.end] == p.text
    assert paragraphs[0].start == 2
    assert len(blocks) == 1


def test_paragraph_ids_stable_when_block_budget_changes():
    text = "甲" * 650 + "\n乙" * 100
    small, small_blocks = split_chapter(text, 200)
    large, large_blocks = split_chapter(text, 2400)
    assert small == large
    assert len(small_blocks) > len(large_blocks)
    assert all(sum(len(p.text) for p in b.paragraphs) <= 200 for b in small_blocks)
    assert "".join(p.text for p in small) == text.replace("\n", "")


@pytest.mark.parametrize("change", ["wrong_quote", "wrong_paragraph", "bad_alias", "occurrence"])
def test_invalid_evidence_leaves_no_published_records(repo, recorder, project, change):
    data = payload()
    if change == "wrong_quote":
        data["facts"][0]["evidence"] = [{"paragraph_id": "p0001", "quote": "不存在"}]
    elif change == "wrong_paragraph":
        data["events"][0]["evidence"] = [{"paragraph_id": "p9999", "quote": TEXT}]
    elif change == "bad_alias":
        data["entities"][0]["aliases"] = ["没出现的别名"]
    else:
        data["facts"][0]["evidence"] = [{"paragraph_id": "p0001", "quote": TEXT, "occurrence": 1}]
    with pytest.raises(StorageError):
        pipeline(repo, recorder, {"b0001": data}).ingest(project, 1, "一", TEXT)
    assert count(repo, "facts") == count(repo, "entities") == 0
    chapter = repo.chapters(project)[0]
    assert chapter["latest_job"]["status"] == "failed"
    assert not chapter["knowledge_is_current"]


def test_model_reference_schema_rejected_before_publish(repo, recorder, project):
    data = payload()
    data["facts"][0]["subject"] = "missing"
    with pytest.raises(LLMError):
        pipeline(repo, recorder, {"b0001": data}).ingest(project, 1, "一", TEXT)
    assert count(repo, "facts") == 0


def test_failure_mid_chapter_resumes_only_unfinished_blocks(repo, recorder, project):
    text = "\n".join([TEXT] * 30)
    _, blocks = split_chapter(text, 200)
    fixtures = {b.block_id: payload(b.paragraphs[0].paragraph_id, b.paragraphs[0].text) for b in blocks}
    first_only = {blocks[0].block_id: fixtures[blocks[0].block_id]}
    with pytest.raises(LLMError):
        pipeline(repo, recorder, first_only, max_chars=200).ingest(project, 1, "一", text)
    assert count(repo, "extraction_blocks") == 1
    assert count(repo, "entities") == 0
    assert repo.chapters(project)[0]["latest_job"]["completed_blocks"] == 1
    with pytest.raises(StorageError, match="--retry"):
        pipeline(repo, recorder, fixtures, max_chars=200).ingest(project, 1, "一", text)
    result = pipeline(repo, recorder, fixtures, max_chars=200).ingest(project, 1, "一", text, retry=True)
    assert result["model_calls"] == len(blocks) - 1
    assert len(recorder.events) == len(blocks)
    assert count(repo, "facts") == len(blocks)


def test_publish_transaction_rolls_back_and_can_retry_without_model(repo, recorder, project):
    with repo.db.connect() as conn:
        conn.execute("CREATE TRIGGER fail_fact BEFORE INSERT ON facts BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ingest(repo, recorder, project)
    assert count(repo, "entities") == count(repo, "events") == 0
    assert count(repo, "extraction_blocks") == 1
    with repo.db.connect() as conn:
        conn.execute("DROP TRIGGER fail_fact")
    result = ingest(repo, recorder, project, retry=True)
    assert result["model_calls"] == 0
    assert len(recorder.events) == 1
    assert count(repo, "facts") == 1


def test_failed_new_version_retains_old_snapshot_and_marks_stale(repo, recorder, project):
    original = ingest(repo, recorder, project)
    old_fact = repo.records(project, "fact")[0]
    new_text = "林舟没有交出钥匙。"
    with pytest.raises(StorageError):
        pipeline(repo, recorder).ingest(project, 1, "改稿", new_text)
    chapter = repo.chapters(project)[0]
    assert chapter["published_generation_id"] == original["job_id"]
    assert chapter["latest_revision"] == 2 and not chapter["knowledge_is_current"]
    assert repo.records(project, "fact") == []
    assert repo.records(project, "fact", include_stale=True)[0]["id"] == old_fact["id"]
    assert not repo.evidence("fact", old_fact["id"])[0]["is_latest_source"]
    restored = ingest(repo, recorder, project)
    assert restored["cached"]
    assert repo.chapters(project)[0]["knowledge_is_current"]


def test_new_version_publishes_new_snapshot_without_deleting_history(repo, recorder, project):
    ingest(repo, recorder, project)
    original_entities = {e["id"] for e in repo.records(project, "entity")}
    new_text = TEXT + "此事发生在清晨。"
    pipe = pipeline(repo, recorder, {"b0001": payload(quote=new_text)})
    pipe.ingest(project, 1, "改稿", new_text)
    assert count(repo, "facts") == count(repo, "chapter_versions") == 2
    assert len(repo.records(project, "fact")) == 1
    assert repo.chapters(project)[0]["knowledge_is_current"]
    assert count(repo, "entities") == 3
    assert {e["id"] for e in repo.records(project, "entity")} == original_entities


def test_active_claim_blocks_duplicate_calls(repo, recorder, project):
    paragraphs, blocks = split_chapter(TEXT)
    pipe = pipeline(repo, recorder)
    job = repo.prepare(project, 1, "一", TEXT, paragraphs, blocks, pipe.signature)
    repo.claim(job["id"], retry=False, lease_seconds=900)
    with pytest.raises(StorageError, match="正在运行"):
        pipe.ingest(project, 1, "一", TEXT, retry=True)
    assert recorder.events == []


def test_expired_claim_requires_retry_and_can_resume(repo, recorder, project):
    paragraphs, blocks = split_chapter(TEXT)
    pipe = pipeline(repo, recorder)
    job = repo.prepare(project, 1, "一", TEXT, paragraphs, blocks, pipe.signature)
    repo.claim(job["id"], retry=False, lease_seconds=-1)
    result = pipe.ingest(project, 1, "一", TEXT, retry=True)
    assert result["status"] == "published"


def test_old_inflight_version_cannot_overwrite_newer_source(repo, recorder, project):
    pipe = pipeline(repo, recorder)
    def changed_while_extracting(event):
        paragraphs, blocks = split_chapter(TEXT + "改稿")
        repo.prepare(project, 1, "新稿", TEXT + "改稿", paragraphs, blocks, "different")
    with pytest.raises(StorageError, match="更新版本"):
        pipe.ingest(project, 1, "一", TEXT, progress=changed_while_extracting)
    assert count(repo, "facts") == 0


def test_edit_updates_memory_and_invalidates_summary(repo, recorder, project):
    ingest(repo, recorder, project)
    fact = repo.records(project, "fact")[0]
    preview = repo.impact("fact", fact["id"])
    assert preview["summaries_will_be_stale"]
    result = repo.edit("fact", fact["id"], field="object_value", value="林舟", reason="作者修正")
    assert result["record"]["object_entity_id"] is None
    assert repo.records(project, "fact")[0]["object_value"] == "林舟"
    assert repo.records(project, "summary") == []
    assert repo.evidence("fact", fact["id"])[0]["quote"] == TEXT
    with repo.db.connect() as conn:
        assert "林舟" in conn.execute("SELECT text FROM memory_items WHERE record_id=?", (fact["id"],)).fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM edit_history").fetchone()[0] == 1
    ingest(repo, recorder, project)
    assert repo.records(project, "fact")[0]["object_value"] == "林舟"


def test_rejected_fact_hidden_and_manual_summary_rebuild(repo, recorder, project):
    ingest(repo, recorder, project)
    fact = repo.records(project, "fact")[0]
    summary = repo.records(project, "summary")[0]
    repo.edit("fact", fact["id"], status="rejected", reason="误提取")
    assert repo.records(project, "fact") == []
    repo.edit("summary", summary["id"], field="text", value="作者修订摘要", reason="同步修订")
    assert repo.records(project, "summary")[0]["text"] == "作者修订摘要"


def test_distinct_entities_can_be_merged_without_losing_sources(repo, recorder, project):
    pipe = pipeline(repo, recorder, {"b0001": payload(distinct=True)})
    pipe.ingest(project, 1, "一", TEXT)
    pipe.ingest(project, 2, "二", TEXT)
    people = [row for row in repo.records(project, "entity") if row["name"] == "林舟"]
    assert len(people) == 2
    result = repo.merge(people[0]["id"], people[1]["id"], "作者确认同一身份")
    assert result["target_id"] == people[1]["id"]
    assert len([row for row in repo.records(project, "entity") if row["name"] == "林舟"]) == 1
    assert len(repo.evidence("entity", people[1]["id"])) == 2
    with pytest.raises(StorageError):
        repo.merge(people[1]["id"], people[0]["id"], "不可反向构成环")


def test_cross_project_merge_is_blocked(repo, recorder, project):
    ingest(repo, recorder, project)
    other = repo.create_project("另一小说")["id"]
    ingest(repo, recorder, other)
    a = repo.records(project, "entity")[0]
    b = repo.records(other, "entity")[0]
    with pytest.raises(StorageError, match="同一项目"):
        repo.merge(a["id"], b["id"], "错误合并")


def test_alias_author_confirmation_survives_reimport(repo, recorder, project):
    ingest(repo, recorder, project)
    entity = next(row for row in repo.records(project, "entity") if row["name"] == "林舟")
    repo.add_alias(entity["id"], "阿舟", "作者设定")
    assert "阿舟" in next(row for row in repo.records(project, "entity") if row["id"] == entity["id"])["aliases"]
    repo.edit("entity", entity["id"], field="name", value="林远舟", reason="更名")
    ingest(repo, recorder, project)
    assert next(row for row in repo.records(project, "entity") if row["id"] == entity["id"])["name"] == "林远舟"


def test_confirm_only_keeps_summary_but_rejection_invalidates_it(repo, recorder, project):
    ingest(repo, recorder, project)
    fact = repo.records(project, "fact")[0]
    repo.edit("fact", fact["id"], status="confirmed", reason="核对无误")
    assert len(repo.records(project, "summary")) == 1
    repo.edit("fact", fact["id"], status="rejected", reason="撤回确认")
    assert repo.records(project, "summary") == []
    assert repo.records(project, "summary", include_stale=True)[0]["is_stale"] == 1


def test_rejected_entity_is_not_used_by_fact_memory(repo, recorder, project):
    ingest(repo, recorder, project)
    entity = next(e for e in repo.records(project, "entity") if e["name"] == "备用钥匙")
    fact = repo.records(project, "fact")[0]
    repo.edit("entity", entity["id"], status="rejected", reason="作者否决此实体")
    assert repo.records(project, "fact") == []
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_items WHERE record_id=?", (fact["id"],)).fetchone()[0] == 0


def test_merged_target_author_alias_is_visible_even_without_own_active_mention(repo, recorder, project):
    pipe = pipeline(repo, recorder, {"b0001": payload(distinct=True)})
    pipe.ingest(project, 1, "一", TEXT)
    pipe.ingest(project, 2, "二", TEXT)
    people = [e for e in repo.records(project, "entity") if e["name"] == "林舟"]
    repo.add_alias(people[1]["id"], "阿舟", "别名")
    repo.merge(people[0]["id"], people[1]["id"], "身份确认")
    merged = next(e for e in repo.records(project, "entity") if e["id"] == people[1]["id"])
    assert "阿舟" in merged["aliases"]


def test_alias_from_superseded_source_does_not_merge_another_person(repo, recorder, project):
    first_text = "阿舟就是林舟。" + TEXT
    first = payload(quote=first_text)
    first["entities"][0]["aliases"] = ["阿舟"]
    pipeline(repo, recorder, {"b0001": first}).ingest(project, 1, "一", first_text)
    pipeline(repo, recorder).ingest(project, 2, "二", TEXT)
    # 第一章改稿，删除自动提取别名；第二章仍使用原身份。
    pipeline(repo, recorder).ingest(project, 1, "一改", TEXT)
    third_text = "阿舟把备用钥匙交给妹妹。"
    third = payload(quote=third_text)
    third["entities"][0]["name"] = "阿舟"
    third["summary"] = "阿舟交接钥匙"
    third["events"][0]["summary"] = "阿舟把钥匙交给妹妹"
    pipeline(repo, recorder, {"b0001": third}).ingest(project, 3, "三", third_text)
    people = [e for e in repo.records(project, "entity") if e["kind"] == "person"]
    assert {p["name"] for p in people} == {"林舟", "阿舟", "妹妹"}


def test_alias_resolves_between_blocks_before_whole_chapter_publish(repo, recorder, project):
    first_text = "林舟又名阿舟。" + TEXT
    second_text = "阿舟把备用钥匙交给妹妹。"
    first = payload(quote=first_text)
    first["entities"][0]["aliases"] = ["阿舟"]
    second = payload("p0003", quote=second_text)
    second["entities"][0]["name"] = "阿舟"
    second["summary"] = "阿舟交接钥匙"
    second["events"][0]["summary"] = "阿舟把钥匙交给妹妹"
    text = first_text + "\n***\n" + second_text
    pipe = pipeline(repo, recorder, {"b0001": first, "b0002": second})
    pipe.ingest(project, 1, "一", text)
    people = [e for e in repo.records(project, "entity") if e["kind"] == "person"]
    assert {p["name"] for p in people} == {"林舟", "妹妹"}
    assert "阿舟" in next(p for p in people if p["name"] == "林舟")["aliases"]


@pytest.mark.parametrize("kind,field", [("fact", "id"), ("event", "generation_id"), ("entity", "merged_into")])
def test_edit_fields_are_whitelisted(repo, recorder, project, kind, field):
    ingest(repo, recorder, project)
    row = repo.records(project, kind)[0]
    with pytest.raises(StorageError):
        repo.edit(kind, row["id"], field=field, value="bad", reason="不允许修改内部标识")
