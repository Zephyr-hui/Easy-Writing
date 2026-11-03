from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from easy_writing.services.planning import DemoPlanningClient
from easy_writing.services.tasks import TaskService
from easy_writing.storage.database import Database
from easy_writing.storage.repository import Repository

APP = Path(__file__).resolve().parents[1] / "app.py"


def click(at, label):
    next(button for button in at.button if button.label == label).click().run(timeout=30)
    assert not at.exception
    return at


def sample_app():
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    at.text_input(key="project_name").set_value("工作台样稿")
    click(at, "创建项目")
    click(at, "导入内置演示章节")
    at.run()
    assert not at.exception
    return at


def test_four_pages_render_and_rerun_never_imports_another_chapter(tmp_path):
    at = sample_app()
    for page in ("知识查看", "创作问答", "检查报告", "项目导入"):
        at.radio(key="page").set_value(page).run()
        assert not at.exception
    repo = Repository(Database(tmp_path / "data/easy_writing.db"))
    project = repo.projects()[0]["id"]
    assert len(repo.chapters(project)) == 1
    assert TaskService(repo).list(project)["total"] == 1


def test_planning_submit_is_once_across_reruns_and_routes_are_viewable(monkeypatch, tmp_path):
    from easy_writing.ui import workbench
    class Client(DemoPlanningClient):
        calls = 0
        def extract_json(self, *args, **kwargs):
            self.calls += 1
            return super().extract_json(*args, **kwargs)
    client = Client()
    monkeypatch.setattr(workbench, "_client", lambda *args, **kwargs: client)
    at = sample_app()
    at.radio(key="page").set_value("创作问答").run()
    at.radio(key="creation_mode").set_value("下一章路线").run()
    at.text_area(key="planning_goal").set_value("让兄妹商议如何进入旧宅")
    click(at, "生成并保存路线")
    assert client.calls == 1
    assert any("路线 1" in e.label for e in at.expander)
    at.run()
    at.radio(key="creation_mode").set_value("已保存计划").run()
    at.run()
    assert not at.exception and client.calls == 1
    repo = Repository(Database(tmp_path / "data/easy_writing.db"))
    project = repo.projects()[0]["id"]
    assert TaskService(repo).list(project)["total"] == 2


def test_research_submit_is_not_repeated_by_page_refresh(monkeypatch, tmp_path):
    from easy_writing.ui import workbench
    from easy_writing.retrieval.workflow import DemoResearchClient
    from easy_writing.usage import JsonlCallRecorder
    class Client(DemoResearchClient):
        calls = 0
        def call_tools(self, *args, **kwargs):
            self.calls += 1
            return super().call_tools(*args, **kwargs)
    client = Client(JsonlCallRecorder(tmp_path / "fake_calls.jsonl"))
    monkeypatch.setattr(workbench, "_client", lambda *args, **kwargs: client)
    at = sample_app()
    at.radio(key="page").set_value("创作问答").run()
    at.text_area(key="research_question").set_value("钥匙由谁持有？")
    click(at, "提交问答")
    assert client.calls == 1
    at.run()
    at.radio(key="page").set_value("知识查看").run()
    at.radio(key="page").set_value("创作问答").run()
    assert not at.exception and client.calls == 1


def test_knowledge_edit_and_check_report_are_local_form_actions(tmp_path):
    at = sample_app()
    at.radio(key="page").set_value("知识查看").run()
    at.selectbox(key="knowledge_kind").set_value("fact").run()
    at.text_input(key="edit_reason").set_value("作者核对确认")
    click(at, "保存知识修改")
    at.radio(key="page").set_value("检查报告").run()
    click(at, "运行当前知识检查")
    assert any(m.label == "待处理疑点" for m in at.metric)
    at.run()
    repo = Repository(Database(tmp_path / "data/easy_writing.db"))
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] == 1


def test_conversation_can_be_reopened_and_deep_mode_resets_after_one_turn(tmp_path):
    from easy_writing.services.conversation import ConversationService

    at = sample_app()
    at.radio(key="page").set_value("创作问答").run()
    click(at, "新建会话")
    deep = next(box for box in at.checkbox if box.label.startswith("深度对话"))
    deep.check()
    at.text_area(key="research_question").set_value("林舟在旧宅做了什么？")
    click(at, "提交问答")
    assert not next(box for box in at.checkbox if box.label.startswith("深度对话")).value
    at.text_area(key="research_question").set_value("他后来呢？")
    click(at, "提交问答")
    reopened = AppTest.from_file(str(APP), default_timeout=30).run()
    reopened.radio(key="page").set_value("创作问答").run()
    assert not reopened.exception and len(reopened.get("chat_message")) == 4
    repo = Repository(Database(tmp_path / "data/easy_writing.db"))
    project = repo.projects()[0]["id"]
    chat = ConversationService(repo)
    turns = chat.turns(project, chat.sessions(project)[0]["id"])
    assert [turn["request"]["deep"] for turn in turns] == [True, False]
    assert turns[1]["result"]["memory"]["focus_id"] == turns[0]["result"]["memory"]["focus_id"]


def test_switching_projects_does_not_show_other_projects_saved_results():
    at = sample_app()
    at.text_input(key="project_name").set_value("另一本小说")
    click(at, "创建项目")
    # 作者可手动选择当前作品；结果键同时绑定数据库与项目。
    options = at.selectbox(key="active_project").options
    assert "另一本小说" in options
    repo = Repository(Database(Path("data/easy_writing.db")))
    other = next(p["id"] for p in repo.projects() if p["name"] == "另一本小说")
    at.selectbox(key="active_project").set_value(other).run()
    at.radio(key="page").set_value("创作问答").run()
    assert not at.exception
    assert not any("离线流程演示" in str(m.value) for m in at.markdown)


def test_missing_key_does_not_leave_a_nonexistent_task_reference():
    at = sample_app()
    at.radio(key="page").set_value("创作问答").run()
    at.radio(key="creation_mode").set_value("下一章路线").run()
    at.text_area(key="planning_goal").set_value("商议调查旧宅")
    at.selectbox(key="planning_provider").set_value("deepseek")
    click(at, "生成并保存路线")
    assert any("未配置" in e.value for e in at.error)
    at.run()
    assert not any("指定记录不存在" in e.value for e in at.error)
    at.selectbox(key="planning_provider").set_value("fake")
    click(at, "生成并保存路线")
    assert not at.error


def test_revision_panel_refresh_and_backup_are_explicit_local_actions(tmp_path):
    at = sample_app()
    assert any(e.label == "改稿与数据维护" for e in at.expander)
    click(at, "刷新当前派生资料")
    assert any("当前资料已刷新" in e.value for e in at.success)
    target = tmp_path / "ui-backup.db"
    next(t for t in at.text_input if t.label == "新输出文件路径").set_value(str(target))
    click(at, "执行数据操作")
    assert target.is_file() and Path(str(target) + ".json").is_file()
    original = target.stat().st_mtime_ns
    at.run()
    assert not at.exception and target.stat().st_mtime_ns == original
    repo = Repository(Database(tmp_path / "data/easy_writing.db"))
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] == 1
