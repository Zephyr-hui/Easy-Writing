"""从空数据目录验证打包入口，外部模型验证须显式启用。"""

import json
import time
from contextlib import nullcontext
from tkinter import Text, Toplevel, filedialog, messagebox, simpledialog, ttk
from types import SimpleNamespace
from unittest.mock import patch

from easy_writing.desktop.backend import DesktopBackend
from easy_writing.services.conversation import ChatRequest
from easy_writing.storage.repository import uid


def run(window, args):
    callback_errors = []
    window.root.report_callback_exception = lambda _, error, __: callback_errors.append(error)

    def idle():
        deadline = time.monotonic() + 60
        while window.future and time.monotonic() < deadline:
            window.root.update()
            time.sleep(.03)
        assert not window.future
        if callback_errors:
            raise callback_errors[0]

    def error(message):
        from easy_writing.storage.database import StorageError
        callback_errors.append(StorageError(message))

    window.error = error
    idle()
    assert window.tabs.index("end") == 6
    assert window.provider_id == "deepseek"
    window.sample()
    idle()
    assert window.provider_id == "deepseek"
    window.provider.current(0)
    project, repo, chat = window.project, window.repo, window.chat
    assert len(repo.chapters(project)) == 1 and window.records
    window.root.geometry("+12000+12000")
    window.root.deiconify()
    window.root.update()

    def edit(value, label, text, action, **kwargs):
        window.edit_long_field(label, value, **kwargs)
        popup = window.root.grab_current()
        assert isinstance(popup, Toplevel) and popup.title() == label
        editor = next(widget for frame in popup.winfo_children() for widget in frame.winfo_children() if isinstance(widget, Text))
        window.show(editor, text, editable=True)
        button = next(widget for frame in popup.winfo_children() for widget in frame.winfo_children()
                      if isinstance(widget, ttk.Button) and widget["text"] == action)
        button.invoke()
        assert window.root.grab_current() is None

    guidance = ("重视人物关系\n保留慢节奏", "保持限知视角\n伏笔逐步揭晓")
    edit(window.chat_goal, "作品目标", guidance[0], "保存", guidance=True)
    edit(window.chat_constraints, "作品约束", guidance[1], "保存", guidance=True, constraints=True)
    edit(window.chat_goal, "作品目标", "取消的文本", "取消", guidance=True)
    edit(window.plan_constraints, "本章约束", "暂缓揭晓，保留刻痕\n保持妹妹视角", "保存", constraints=True)
    window.root.withdraw()
    assert chat.guidance(project) == guidance and window.chat_goal.get() == guidance[0]
    assert "原文依据" in window.knowledge_detail.get("1.0", "end")
    for mode, title in (("story", "故事时间线"), ("narrative", "实际发生"), ("relations", "人物关系")):
        window.knowledge_view(mode)
        idle()
        visible = window.knowledge_detail.get("1.0", "end")
        assert title in visible and "snapshot_id" not in visible
    window.show(window.question, "钥匙由谁持有？", editable=True)
    window.send_question()
    idle()
    assert window.turns[-1]["result"]["answer"]["citations"]
    session = window.sessions[0]["id"]
    original = window.work
    assert len(window.library.open(original)[1].turns(project, session)) == 1
    window.show(window.question, "第一章写了什么内容", editable=True)
    window.send_question()
    idle()
    assert window.turns[-1]["result"]["retrieval"]["strategy"] == "model_first"
    assert window.turns[-1]["result"]["tool_trace"][0]["tool"] == "get_chapter_overview"
    assert window.turns[-1]["result"]["answer"]["citations"]
    window.check()
    idle()
    assert window.reports
    report_text = window.check_detail.get("1.0", "end")
    assert "检查概览" in report_text and "伏笔与检查范围" in report_text and "snapshot_id" not in report_text

    class AdviceClient:
        def extract_json(self, prompt, schema, *, system_prompt):
            assert json.loads(prompt)["thread_sample"]
            return SimpleNamespace(content=schema(overview="建议复核门上的月牙刻痕。", suggestions=["可在后续场景强化线索。"]))

    window.provider.current(1)
    with patch("easy_writing.desktop.window.client", return_value=nullcontext(AdviceClient())):
        window.check()
        idle()
    window.provider.current(0)
    assert "建议复核门上的月牙刻痕" in window.check_detail.get("1.0", "end")
    window.plan_goal.set("让兄妹商量进入旧宅")
    window.generate_plan()
    idle()
    assert window.plan_goal.get() and "\n" in window.plan_constraints.get()
    assert window.plans
    visible = window.plan_detail.get("1.0", "end")
    assert "路线 1 · " in visible and "路线 2 · " in visible and '"routes"' not in visible
    assert len(window.route_pick["values"]) == 2
    window.route_pick.current(1)
    window.select_route()
    assert window.plan_detail.get("section_2", "section_2 lineend").startswith("路线 2 · ")
    askstring = simpledialog.askstring
    try:
        simpledialog.askstring = lambda *_, **__: "验收路线选择"
        window.adopt_plan()
    finally:
        simpledialog.askstring = askstring
    idle()
    assert window.plans[0]["selected_route"] == 1
    assert "路线 2" in window.route_pick.get()
    window.draft_min_chars.set("1")
    window.draft_max_chars.set("2")
    with patch.object(messagebox, "askyesno", return_value=False):
        window.submit_draft()
        assert window.plan_goal.get() and not window.drafts
    with patch.object(messagebox, "askyesno", return_value=True):
        window.submit_draft()
        idle()
    assert window.drafts[0]["status"] == "succeeded" and window.drafts[0]["request"]["route_index"] == 1
    assert window.draft_popup and len(repo.chapters(project)) == 1
    assert window.drafts[0]["request"]["max_chars"] == 2
    assert any("超出目标范围" in widget["text"] for widget in window.draft_popup.winfo_children() if isinstance(widget, ttk.Label))
    assert not window.plan_goal.get() and not window.plan_constraints.get()
    assert window.chat_goal.get() == guidance[0]
    for name, is_chat, label in (("knowledge", False, "小说库"), ("chat", True, "会话库")):
        backup = args.data_dir / f"{name}.bak"
        with patch.object(filedialog, "asksaveasfilename", return_value=str(backup)):
            window.archive(is_chat, False)
            idle()
        assert f"{label}备份完成" in window.archive_detail.get("1.0", "end")
        with patch.object(filedialog, "askopenfilename", return_value=str(backup)), patch.object(
            filedialog, "asksaveasfilename", return_value=str(args.data_dir / f"{name}-restored.db")
        ):
            window.archive(is_chat, True)
            idle()
        assert f"{label}已恢复到新文件" in window.archive_detail.get("1.0", "end")
    restored, restored_chat = window.backend.open(args.data_dir / "knowledge-restored.db", args.data_dir / "chat-restored.db")
    assert restored.chapters(project) and restored_chat.turns(project, session)
    checks = ["window", "offline_sample", "chapter_overview", "knowledge_text", "conversation_reopened", "report", "planning",
              "numbered_route_adoption", "separate_restore", "modal_multiline_editing", "selected_route_chapter_draft",
              "readable_report_and_advice", "chapter_length_range"]
    window.plan_goal.set("切换作品时清除")
    window.plan_constraints.set("临时章约束")
    with patch.object(simpledialog, "askstring", return_value="第二部作品"):
        window.new_project()
        idle()
    other = window.work
    assert other != original and not window.repo.chapters(window.project) and not window.sessions
    assert not window.plan_goal.get() and not window.plan_constraints.get()
    assert not window.chat_goal.get() and not window.chat_constraints.get()
    assert window.repo.db.path.parent != repo.db.path.parent
    with patch.object(simpledialog, "askstring", return_value="第二部作品新名"):
        window.rename_work()
        idle()
    assert window.work == other and "第二部作品新名" in window.project_pick.get()
    with patch.object(messagebox, "askyesno", return_value=False):
        window.delete_work()
        assert window.work == other
    with patch.object(messagebox, "askyesno", return_value=True):
        window.delete_work()
        idle()
    assert window.work == original and window.library.info(other, recycled=True)
    with patch.object(simpledialog, "askinteger", return_value=1):
        window.restore_work()
        idle()
    assert window.work == other
    window.refresh_projects(original)
    idle()
    assert (window.chat_goal.get(), window.chat_constraints.get()) == guidance
    with patch.object(simpledialog, "askstring", return_value="验收临时会话"):
        window.new_session()
        idle()
    temporary = window.sessions[window.session_pick.current()]["id"]
    with patch.object(simpledialog, "askstring", return_value="临时会话新名"):
        window.rename_session()
        idle()
    assert window.sessions[window.session_pick.current()]["id"] == temporary
    assert "临时会话新名" in window.session_pick.get()
    with patch.object(messagebox, "askyesno", return_value=False):
        window.delete_session()
        assert len(window.sessions) == 2
    with patch.object(messagebox, "askyesno", return_value=True):
        window.delete_session()
        idle()
    assert [row["id"] for row in window.sessions] == [session] and len(window.turns) == 2
    assert (window.chat_goal.get(), window.chat_constraints.get()) == guidance
    with patch.object(filedialog, "askopenfilename", side_effect=[str(args.data_dir / "knowledge.bak"),
                                                                 str(args.data_dir / "chat.bak")]):
        window.migrate_workspace()
        idle()
    assert "已迁移" in window.archive_detail.get("1.0", "end")
    assert len(window.projects) == 3 and window.work == original
    copied = next(row for row in window.projects if row["work_id"] != original and row["id"] == project)
    copied_repo, copied_chat = window.library.open(copied["work_id"])
    assert copied_chat.guidance(project) == guidance
    from easy_writing.services.chapter_drafting import ChapterDraftService
    assert ChapterDraftService(copied_repo).list(project)[0]["status"] == "succeeded"
    window.revisions(False)
    idle()
    detail = window.archive_detail.get("1.0", "end")
    assert "需刷新摘要 1 章" in detail and "pending_total" not in detail
    window.revisions(True)
    idle()
    assert "已重建第 1 章的摘要" in window.archive_detail.get("1.0", "end")
    export = args.data_dir / "knowledge.json"
    with patch.object(filedialog, "asksaveasfilename", return_value=str(export)):
        window.export_knowledge()
        idle()
    detail = window.archive_detail.get("1.0", "end")
    assert "作品知识已导出" in detail and "record_counts" not in detail and export.is_file()
    checks.extend(("work_isolation", "work_rename_recycle_restore", "session_rename_delete", "paired_backup_import",
                   "project_guidance_isolation", "deepseek_default", "readable_data_settings"))
    if args.smoke_samples:
        backend = DesktopBackend(args.data_dir, args.smoke_env)
        for name, fixture, expected in (
            ("chapter.docx", "word.json", "docx"), ("chapter.pdf", "pdf.json", "pdf"),
        ):
            doc = backend.preview(args.smoke_samples / name)
            assert doc.format == expected and doc.text
            work = window.library.create("发布验收 " + expected)
            imported, _ = window.library.open(work["work_id"])
            project_id = work["id"]
            backend.ingest(imported, project_id, 1, doc.title, doc.text, doc, "fake", args.smoke_samples / fixture, False, lambda _: None)
            event = imported.records(project_id, "event")[0]
            assert imported.evidence("event", event["id"])[0]["document_sources"][0]["format"] == expected
            checks.append(expected + "_import")
        scan = args.smoke_samples / "scan.pdf"
        if scan.exists():
            doc = backend.preview(scan, ocr=True)
            assert doc.pages[0].method == "ocr" and doc.text
            checks.append("local_ocr")
    if args.smoke_python:
        backend = DesktopBackend(args.data_dir, args.smoke_env)
        result = backend.semantic_ask(repo, chat, project, session,
                                      ChatRequest(question="钥匙现在由谁持有？", embedding="bge"), args.smoke_python)
        assert result["metrics"]["semantic"] and result["answer"]["citations"]
        checks.append("local_bge")
    if args.smoke_online:
        backend = DesktopBackend(args.data_dir, args.smoke_env)
        backend.settings = backend.settings.model_copy(update={"usage_log": args.data_dir / "online_calls.jsonl",
                                                               "retrieval_log": args.data_dir / "online_retrieval.jsonl"})
        request = ChatRequest(question="第一章写了什么内容？请简短回答并引用证据。", provider="deepseek")
        result = backend.ask(chat, project, chat.create(project, "接口验收 " + uid()[:6]), request, output_tokens=256)
        metrics = result["metrics"]
        assert metrics["real_model_calls"] and result["answer"]["citations"], metrics.get("stop_reason")
        assert any(item["tool"] == "get_chapter_overview" and item["status"] == "success" for item in result["tool_trace"])
        checks.append("deepseek")
    args.smoke_test.write_text(json.dumps({"ok": True, "checks": checks}, ensure_ascii=False), encoding="utf-8")
    window.close()
    return 0
