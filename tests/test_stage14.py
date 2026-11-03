import json

import pytest

from easy_writing.desktop.backend import DesktopBackend
from easy_writing.desktop.library import WorkLibrary
from easy_writing.llm.base import LLMError
from easy_writing.services.chapter_drafting import ChapterDraftRequest, ChapterDraftService, DemoChapterClient, prose_length
from easy_writing.services.conversation import ChatRequest
from easy_writing.services.planning import DemoPlanningClient, PlanningService
from easy_writing.storage.chat_database import ChatDatabase
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import uid
from easy_writing.ui.sample import import_sample


@pytest.fixture
def sample_work(tmp_path, recorder):
    with WorkLibrary(tmp_path / "works") as library:
        work = library.create("十四阶段", initialize=lambda repo, project: import_sample(repo, project, recorder))
        yield library, work, *library.open(work["work_id"])


def test_guidance_v1_upgrade_and_scope(sample_work, tmp_path):
    library, work, repo, chat = sample_work
    project = work["id"]
    backend = DesktopBackend(tmp_path)
    old_session = chat.create(project, "旧会话")
    backend.ask(chat, project, old_session, ChatRequest(question="第一章写了什么？"))
    with chat.db.transaction() as conn:
        conn.execute("DROP TABLE project_guidance")
        conn.execute("PRAGMA user_version=1")
    assert ChatDatabase.verify(chat.db.path)["schema_version"] == 1
    _, chat = library.open(work["work_id"])
    assert chat.guidance(project) == ("", "") and chat.turns(project, old_session)
    guidance = "慢节奏\n以人物关系推进", "独有旧约束\n保持角色视角"
    chat.save_guidance(project, *guidance)
    _, chat = library.open(work["work_id"])
    assert chat.guidance(project) == guidance
    session = chat.create(project, "同部作品的新会话")
    req = ChatRequest(question="第一章有什么创作建议？", goal=guidance[0], constraints=guidance[1], project_guidance=True)
    result = backend.ask(chat, project, session, req)
    assert (result["memory"]["goal"], result["memory"]["constraints"]) == guidance
    chat.save_guidance(project, "", "")
    result = backend.ask(chat, project, session, ChatRequest(question="第一章有什么建议？", project_guidance=True,
                                                              goal="", constraints=""))
    assert "独有旧约束" not in json.dumps(result["memory"], ensure_ascii=False)
    assert result["memory"]["goal"] == result["memory"]["constraints"] == ""
    assert not result["memory"]["recent"] and not result["memory"]["rolling"]
    chat.save_guidance(project, *guidance)
    chat.delete(project, session)
    other = library.create("另一部作品")
    assert library.open(other["work_id"])[1].guidance(other["id"]) == ("", "")
    assert chat.guidance(project) == guidance


def test_draft_persists_request_and_retries_without_changing_story(sample_work):
    library, work, repo, _ = sample_work
    project = work["id"]
    planning = PlanningService(repo)
    event = repo.records(project, "event")[0]
    actors = [entity["id"] for entity in repo.records(project, "entity") if entity["kind"] == "person"]
    plan = planning.generate(project, uid(), {"chapter_no": 2, "goal": "候选目标", "actors": actors,
                             "at_event": event["id"]}, DemoPlanningClient())["result"]["plan_id"]
    request = ChapterDraftRequest(route_index=1, provider="fake", goal="让两人产生分歧\n保留悬念",
                                  constraints=["暂缓揭晓，保留月牙刻痕", "切到林月视角"], min_chars=20, max_chars=80)
    service = ChapterDraftService(repo)
    with pytest.raises(StorageError, match="有效的路线"):
        service.submit(project, plan, request.model_copy(update={"route_index": 99}))
    draft = service.submit(project, plan, request)
    assert service.get(project, draft)["status"] == "pending"
    chapters = repo.chapters(project)

    class Truncated(DemoChapterClient):
        def extract_json(self, *_, **__):
            raise LLMError("输出被截断", code="truncated")

    with pytest.raises(LLMError, match="截断"):
        service.generate(project, draft, lambda _: Truncated())
    assert service.get(project, draft)["error_code"] == "truncated"

    class InspectPrompt(DemoChapterClient):
        def extract_json(self, prompt, schema, **kwargs):
            packet = json.loads(prompt)
            assert packet["chapter_requirements"] == {"goal": request.goal, "constraints": request.constraints,
                                                       "length": {"min_chars": 20, "max_chars": 80}}
            assert packet["excerpts"] and packet["selected_route"]["title"] == planning.get(project, plan)["response"]["routes"][1]["title"]
            return super().extract_json(prompt, schema, **kwargs)

    reopened, _ = library.open(work["work_id"])
    service = ChapterDraftService(reopened)
    result = service.generate(project, draft, lambda _: InspectPrompt())
    assert result["title"] and "离线演示草稿" in result["body"]
    assert service.get(project, draft)["request"]["max_chars"] == 80
    assert service.generate(project, draft, lambda _: pytest.fail("已成功的草稿不能再次调用模型")) == result
    assert repo.chapters(project) == chapters and planning.get(project, plan)["status"] == "draft"
    repo.edit("event", event["id"], status="confirmed", reason="重新确认事件")
    with pytest.raises(StorageError, match="知识已变化"):
        service.submit(project, plan, request)


def test_optional_draft_length_validation():
    required = {"route_index": 0, "provider": "fake"}
    assert ChapterDraftRequest(**required).min_chars is None
    assert prose_length(" 甲，乙。\n\n丙") == 5
    for bounds in ({"min_chars": 3}, {"max_chars": 3}, {"min_chars": 3, "max_chars": 2},
                   {"min_chars": 0, "max_chars": 2}, {"min_chars": 3, "max_chars": 12001}):
        with pytest.raises(ValueError):
            ChapterDraftRequest(**required, **bounds)
