from pathlib import Path

from easy_writing.desktop.backend import DesktopBackend
from easy_writing.retrieval.embeddings import FakeEmbeddingProvider
from easy_writing.services.conversation import ChatRequest
from easy_writing.ui.clients import recorder
from easy_writing.ui.sample import import_sample


def test_desktop_workspace_reopens_separate_libraries_and_indexes_character_details(tmp_path):
    backend = DesktopBackend(tmp_path / "user")
    repo, chat = backend.open()
    assert repo.db.path == tmp_path / "user" / "data" / "easy_writing.db"
    assert chat.db.path != repo.db.path
    project = repo.create_project("桌面验收")["id"]
    import_sample(repo, project, recorder(backend.settings))
    session = chat.create(project, "查询人物")
    result = backend.ask(chat, project, session, ChatRequest(question="林舟是谁？", embedding="bge"),
                         embedding=FakeEmbeddingProvider())
    assert result["metrics"]["semantic"]
    assert result["answer"]["citations"]
    restored_repo, restored_chat = backend.open(Path(repo.db.path), Path(chat.db.path))
    assert restored_repo.chapters(project)[0]["knowledge_is_current"]
    assert restored_chat.turns(project, session)[0]["result"]["answer"] == result["answer"]
