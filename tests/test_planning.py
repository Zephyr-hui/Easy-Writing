import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.services.planning import DemoPlanningClient, PlanDraft, PlanningService
from easy_writing.services.tasks import TaskService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid
from easy_writing.ui.sample import import_sample


@pytest.fixture
def plan_book(tmp_path, recorder):
    repo = Repository(Database(tmp_path / "plans.db"))
    project = repo.create_project("路线测试")["id"]
    imported = import_sample(repo, project, recorder)
    actors = [e["id"] for e in repo.records(project, "entity") if e["kind"] == "person"]
    request = {"chapter_no": 2, "goal": "进入旧宅并强化划痕的悬念", "actors": actors, "constraints": ["不要让林月提前知道密码"], "at_event": imported["event_id"]}
    return dict(repo=repo, project=project, request=request, service=PlanningService(repo), recorder=recorder)


class CountingClient(DemoPlanningClient):
    def __init__(self, transform=None):
        self.calls = 0
        self.transform = transform

    def extract_json(self, prompt, schema, **kwargs):
        self.calls += 1
        result = super().extract_json(prompt, schema, **kwargs)
        if self.transform:
            data = result.content.model_dump()
            self.transform(data, json.loads(prompt))
            result.content = PlanDraft.model_validate(data)
        return result


def test_routes_persist_and_replayed_task_does_not_call_model_or_publish_facts(plan_book):
    b = plan_book
    client, task = CountingClient(), uid()
    before = {k: b["repo"].records(b["project"], k) for k in ("event", "fact", "thread")}
    first = b["service"].generate(b["project"], task, b["request"], client)
    second = b["service"].generate(b["project"], task, b["request"], client)
    assert client.calls == 1 and second["cached"] and first["result"] == second["result"]
    plan = b["service"].get(b["project"], first["result"]["plan_id"])
    assert len(plan["response"]["routes"]) == 2 and plan["status"] == "draft" and not plan["stale"]
    assert plan["metrics"]["real_model_calls"] == 0 and not plan["plans_are_facts"]
    for kind, rows in before.items():
        assert b["repo"].records(b["project"], kind) == rows


def test_actor_packet_separates_author_secret_from_character_knowledge(plan_book):
    b = plan_book
    packet = b["service"].context(b["project"], b["request"])
    secret = next(f["key"] for f in packet["facts"] if f["predicate"] == "密码")
    known = {a["name"]: a["known_keys"] for a in packet["actors"]}
    assert secret in known["林舟"] and secret not in known["林月"]
    assert packet["world_rules"] and packet["world_rules"][0]["value"] == "成功"
    assert all("quote" not in ref for f in packet["facts"] for ref in f["evidence"])


def test_unknown_information_cannot_be_used_and_failed_task_is_not_retried(plan_book):
    b = plan_book
    def violate(data, packet):
        actor = next(a["id"] for a in packet["actors"] if a["name"] == "林月")
        secret = next(f["key"] for f in packet["facts"] if f["predicate"] == "密码")
        data["routes"][0]["steps"][0]["uses"] = [{"actor_id": actor, "fact_key": secret}]
    client, task = CountingClient(violate), uid()
    with pytest.raises(StorageError, match="未获知"):
        b["service"].generate(b["project"], task, b["request"], client)
    with pytest.raises(StorageError, match="不会自动重发"):
        b["service"].generate(b["project"], task, b["request"], client)
    assert client.calls == 1 and b["service"].list(b["project"])["total"] == 0
    failed = TaskService(b["repo"]).result(b["project"], task)
    assert failed["status"] == "failed" and failed["result"]["adoptable"] is False
    assert failed["result"]["metrics"]["call_id"] and "林月" in failed["result"]["validation_error"]


def test_planned_learning_can_precede_use_but_does_not_update_real_awareness(plan_book):
    b = plan_book
    def setup(data, packet):
        source = next(a["id"] for a in packet["actors"] if a["name"] == "林舟")
        target = next(a["id"] for a in packet["actors"] if a["name"] == "林月")
        secret = next(f["key"] for f in packet["facts"] if f["predicate"] == "密码")
        data["routes"][0]["steps"][0]["learns"] = [{"actor_id": target, "source_actor_id": source, "fact_key": secret}]
        data["routes"][0]["steps"][1]["uses"] = [{"actor_id": target, "fact_key": secret}]
    result = b["service"].generate(b["project"], uid(), b["request"], CountingClient(setup))
    assert result["result"]["plan_id"]
    packet = b["service"].context(b["project"], b["request"])
    moon = next(a for a in packet["actors"] if a["name"] == "林月")
    secret = next(f["key"] for f in packet["facts"] if f["predicate"] == "密码")
    assert secret not in moon["known_keys"]


@pytest.mark.parametrize("violation", ["rule", "citation", "arc", "unknown_subject", "duplicate", "participation"])
def test_invalid_routes_are_not_saved(plan_book, violation):
    b = plan_book
    def mutate(data, packet):
        route = data["routes"][0]
        if violation == "rule":
            rule = packet["world_rules"][0]
            route["steps"][0]["assertions"] = [{"subject_id": rule["subject_id"], "predicate": rule["predicate"], "value": rule["value"]}]
        elif violation == "citation":
            route["citations"] = ["fact:not-in-packet"]
        elif violation == "arc":
            route["character_arcs"].pop()
        elif violation == "unknown_subject":
            route["steps"][0]["assertions"] = [{"subject_id": "another-book", "predicate": "穿墙", "value": "成功"}]
        elif violation == "participation":
            for step in route["steps"]:
                step["actors"] = step["actors"][:1]
        else:
            data["routes"][1] = route
    with pytest.raises(StorageError):
        b["service"].generate(b["project"], uid(), b["request"], CountingClient(mutate))
    assert b["service"].list(b["project"])["total"] == 0


def test_adoption_is_independent_and_written_requires_current_target_publication(plan_book):
    b = plan_book
    result = b["service"].generate(b["project"], uid(), b["request"], CountingClient())
    plan_id = result["result"]["plan_id"]
    with pytest.raises(StorageError, match="先明确采纳"):
        b["service"].decide(b["project"], plan_id, "written", reason="不能跳过采纳")
    adopted = b["service"].decide(b["project"], plan_id, "adopted", route_index=1, reason="作者选择调查路线")
    assert adopted["status"] == "adopted" and not adopted["stale"]
    with pytest.raises(StorageError, match="目标章节"):
        b["service"].decide(b["project"], plan_id, "written", version_id=b["repo"].chapters(b["project"])[0]["latest_version_id"], reason="错章不能关联")
    text = "林舟和林月商议调查划痕。"
    payload = {"summary": text, "summary_evidence": [{"paragraph_id": "p0001", "quote": text}]}
    IngestionPipeline(b["repo"], FixtureLLMClient(b["recorder"], {"b0001": payload}), model_identity="written-fixture").ingest(b["project"], 2, "作者写入的正文", text)
    version = next(c["latest_version_id"] for c in b["repo"].chapters(b["project"]) if c["chapter_no"] == 2)
    written = b["service"].decide(b["project"], plan_id, "written", version_id=version, reason="作者确认此版正文实现所选路线")
    assert written["status"] == "written" and written["written_version_id"] == version and len(written["history"]) == 2
    assert len(b["repo"].records(b["project"], "fact")) == 3  # 不把路线描述复制成事实。


def test_stale_plan_cannot_be_adopted_and_cross_project_ids_are_rejected(plan_book):
    b = plan_book
    plan_id = b["service"].generate(b["project"], uid(), b["request"], CountingClient())["result"]["plan_id"]
    fact = b["repo"].records(b["project"], "fact")[0]
    b["repo"].edit("fact", fact["id"], field="object_value", value="作者修订", reason="更正依据")
    assert b["service"].get(b["project"], plan_id)["stale"]
    with pytest.raises(StorageError, match="过期"):
        b["service"].decide(b["project"], plan_id, "adopted", route_index=0, reason="应先重做")
    other = b["repo"].create_project("别的作品")["id"]
    with pytest.raises(StorageError):
        b["service"].get(other, plan_id)


def test_task_lock_blocks_simultaneous_duplicate_operations(plan_book):
    b = plan_book
    service, task = TaskService(b["repo"]), uid()
    entered, release = Event(), Event()
    def operation():
        entered.set()
        assert release.wait(10)
        return {"once": True}
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.perform, b["project"], task, "research", {"q": 1}, operation)
        assert entered.wait(10)
        try:
            with pytest.raises(StorageError, match="不会自动重发"):
                service.perform(b["project"], task, "research", {"q": 1}, operation)
        finally:
            release.set()
        assert first.result()["result"] == {"once": True}
    with pytest.raises(StorageError, match="绑定"):
        service.perform(b["project"], task, "research", {"q": 2}, lambda: {})


def test_budget_and_existing_chapter_refuse_before_model_call(plan_book):
    b = plan_book
    client = CountingClient()
    with pytest.raises(StorageError, match="输入预算"):
        b["service"].generate(b["project"], uid(), b["request"], client, input_budget=4000)
    with pytest.raises(StorageError, match="章节号"):
        b["service"].generate(b["project"], uid(), {**b["request"], "chapter_no": 1}, client)
    assert client.calls == 0


def test_open_thread_is_not_forced_into_payoff(plan_book):
    from easy_writing.services.foreshadowing import ForeshadowingService
    b = plan_book
    thread = b["repo"].records(b["project"], "thread")[0]
    ForeshadowingService(b["repo"]).transition(b["project"], thread["id"], "open", reason="作者保留开放解释")
    def mutate(data, packet):
        data["routes"][0]["thread_impacts"] = [{"thread_key": packet["threads"][0]["key"], "action": "payoff", "explanation": "不应强行解释"}]
    with pytest.raises(StorageError, match="伏笔意图"):
        b["service"].generate(b["project"], uid(), b["request"], CountingClient(mutate))


def test_same_task_cannot_change_model_configuration(plan_book, settings):
    b = plan_book
    client, task = CountingClient(), uid()
    client.settings = settings
    b["service"].generate(b["project"], task, b["request"], client)
    client.settings = settings.model_copy(update={"extraction_model": "another-model"})
    with pytest.raises(StorageError, match="绑定"):
        b["service"].generate(b["project"], task, b["request"], client)
    assert client.calls == 1


def test_change_during_generation_prevents_mixed_snapshot_plan(plan_book):
    b = plan_book
    def edit_during_call(data, packet):
        fact = b["repo"].records(b["project"], "fact")[0]
        b["repo"].edit("fact", fact["id"], field="object_value", value="新稿值", reason="生成期间改稿")
    with pytest.raises(StorageError, match="知识已变化"):
        b["service"].generate(b["project"], uid(), b["request"], CountingClient(edit_during_call))
    assert b["service"].list(b["project"])["total"] == 0


def test_v4_upgrade_preserves_project_and_backup(tmp_path):
    from importlib.resources import files
    path = tmp_path / "legacy-v4.db"
    with sqlite3.connect(path) as conn:
        for name in ("schema.sql", "migration_002.sql", "migration_003.sql", "migration_004.sql"):
            conn.executescript(files("easy_writing.storage").joinpath(name).read_text(encoding="utf-8"))
        conn.execute("INSERT INTO projects VALUES('legacy','升级样稿','old')")
        conn.execute("PRAGMA user_version=4")
    db = Database(path)
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Database.VERSION
        assert conn.execute("SELECT name FROM projects").fetchone()[0] == "升级样稿"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(str(path) + ".pre-v5.bak") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_author_can_recheck_rejected_draft_after_learning_annotation_without_model_call(plan_book):
    from easy_writing.services.knowledge import KnowledgeService
    b = plan_book
    def unknown(data, packet):
        moon = next(a["id"] for a in packet["actors"] if a["name"] == "林月")
        secret = next(f["key"] for f in packet["facts"] if f["predicate"] == "密码")
        data["routes"][0]["steps"][0]["uses"] = [{"actor_id": moon, "fact_key": secret}]
    client, failed_task = CountingClient(unknown), uid()
    with pytest.raises(StorageError):
        b["service"].generate(b["project"], failed_task, b["request"], client)
    secret = next(f["id"] for f in b["repo"].records(b["project"], "fact") if f["predicate"] == "密码")
    KnowledgeService(b["repo"]).learn(secret, "林月", b["request"]["at_event"], reason="作者补记确有更早渠道")
    result = b["service"].recheck(b["project"], uid(), failed_task, reason="只重审已有草稿")
    plan = b["service"].get(b["project"], result["result"]["plan_id"])
    assert client.calls == 1 and plan["metrics"]["real_model_calls"] == 0
    assert plan["metrics"]["original_call_id"] and not plan["stale"]
    b["repo"].edit("fact", secret, field="object_value", value="新密码", reason="正文已变")
    with pytest.raises(StorageError, match="不能复用"):
        b["service"].recheck(b["project"], uid(), failed_task, reason="不能把旧路线套上新密码")


def test_plot_subject_must_resolve_to_unique_existing_thread(plan_book):
    from easy_writing.services.foreshadowing import ForeshadowingService
    b = plan_book
    def annotate_thread(data, packet):
        data["routes"][0]["steps"][0]["assertions"] = [{"subject_id": "月牙划痕", "predicate": "外观", "value": "出现尚待核对的粉末"}]
    result = b["service"].generate(b["project"], uid(), b["request"], CountingClient(annotate_thread))
    plan = b["service"].get(b["project"], result["result"]["plan_id"])
    assert plan["response"]["routes"][0]["steps"][0]["assertions"][0]["subject_id"].startswith("thread:")
    ForeshadowingService(b["repo"]).create(b["project"], "月牙划痕", "另一处同名线索", event_id=b["request"]["at_event"], reason="测试同名歧义")
    with pytest.raises(StorageError, match="歧义"):
        b["service"].generate(b["project"], uid(), b["request"], CountingClient(annotate_thread))
